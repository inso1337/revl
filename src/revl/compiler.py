"""Pipeline: source files -> parse -> check/lower -> link -> IR document."""

from __future__ import annotations

import ast
import os
import textwrap
from dataclasses import dataclass, field

from . import parser as _ast
from ._paths import backends_root, stdlib_root
from .admit_profile import AdmissionProfile
from .admit_profile import check_no_extern as _check_no_extern
from .admit_profile import check_no_host_extern_reach as _check_no_host_extern_reach
from .admit_profile import enforce_document as _enforce_document
from .admit_profile import enforce_source as _enforce_source
from .errors import RevlError
from .holes import refuse_admission
from .hostfile import _contained
from .hostfile import program_has_body_file as _program_has_body_file
from .hostfile import resolve_body_files as _resolve_body_files
from .hostref import program_has_ref as _program_has_ref
from .hostref import resolve_refs as _resolve_refs
from .lower import check_and_lower
from .parser import ExternDecl, FnDecl, Parser, Program, ServiceDecl, TypeDecl, parse_file
from .typecheck import format_type, parse_type


def _default_search_path() -> list[str]:
    """The fallback directories a `use` path is tried against when it does
    not resolve relative to the importing file (roadmap 319).

    `REVL_IMPORT_PATH` (like `PYTHONPATH`: entries joined by `os.pathsep`,
    tried in the given order) lets a consumer add its own module roots — it
    is checked first, so it can even point at a stand-in stdlib for testing.
    The revl stdlib's parent directory is appended last as the standing
    default, so a bare `use "stdlib/fs.rvl"` resolves for any consumer
    without vendoring a byte-copy of the stdlib into its own tree: the
    literal `stdlib/...` the module writes joins onto this parent to reach
    the real file.
    """
    path = []
    for entry in os.environ.get("REVL_IMPORT_PATH", "").split(os.pathsep):
        entry = entry.strip()
        if entry:
            path.append(entry)
    path.append(str(stdlib_root().parent))
    return path


def _backend_python_module_names() -> set[str]:
    """Return importable top-level names from the Python backend tree.

    Inline Python in user modules must not be able to turn the runtime's
    backend search path into an undeclared host dependency.  The compiler
    checks names rather than relying on the runtime path layout, so the refusal
    also applies to generated artifacts loaded by another process.
    """
    root = backends_root() / "python"
    return {path.stem for path in root.glob("*.py") if path.stem != "__init__"}


def _check_user_py_body_imports(program: Program, install_origin: bool) -> None:
    """Refuse backend imports in inline `@py` bodies from user modules."""
    if install_origin:
        return
    backend_names = _backend_python_module_names()
    for extern in program.externs:
        for body in extern.bodies:
            if not isinstance(body, _ast.HostBody) or body.backend != "py":
                continue
            try:
                tree = ast.parse(textwrap.dedent(body.text), mode="exec")
            except SyntaxError:
                # The Python backend reports body syntax errors at emission;
                # do not replace that diagnostic with an import-policy error.
                continue
            for node in ast.walk(tree):
                imported: list[str] = []
                if isinstance(node, ast.Import):
                    imported = [alias.name.split(".", 1)[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = [node.module.split(".", 1)[0]]
                offender = next((name for name in imported if name in backend_names), None)
                if offender is not None:
                    raise RevlError(
                        program.filename,
                        body.line + max(node.lineno - 1, 0),
                        f"user-origin @py body for extern `{extern.name}` imports "
                        f"backend module `{offender}`",
                        hint="the user @py backend-import rule refuses ambient "
                             "backends/python access; declare trusted host code "
                             "with `@py ref` instead",
                    )


@dataclass
class _LoadedModule:
    path: str
    dir: str
    program: Program
    public_fns: dict[str, FnDecl] = field(default_factory=dict)
    public_types: dict[str, TypeDecl] = field(default_factory=dict)
    public_externs: dict[str, ExternDecl] = field(default_factory=dict)
    services: dict[str, ServiceDecl] = field(default_factory=dict)
    named_fns: set[str] = field(default_factory=set)
    named_types: set[str] = field(default_factory=set)
    named_externs: set[str] = field(default_factory=set)
    named_services: set[str] = field(default_factory=set)
    aliases: dict[str, "_LoadedModule"] = field(default_factory=dict)
    pure_dependencies: set[int] = field(default_factory=set)


def escaping_use_path(path: str) -> bool:
    """Whether a `use` path names a file outside the importing directory's own
    tree: an ABSOLUTE path, or a relative one that normalises to a leading
    `..`. Purely syntactic, so the check opens and stat-s nothing. Everything
    else resolves under the importer's directory or, when nothing sits there,
    through the operator's own search path (`REVL_IMPORT_PATH`, then the
    installed stdlib), which is why `use "stdlib/str.rvl"` is never escaping.

    Shared by the MCP transport jail (`mcp.server._escaping_use`, roadmap 425
    F2) and the in-compiler admission confinement below, so the two doors
    agree on what "escaping" means."""
    if os.path.isabs(path):
        return True
    normalized = os.path.normpath(path)
    return normalized == ".." or normalized.startswith(".." + os.sep)


class _ModuleLoader:
    """Loads modules and resolves `use` with cycle detection.

    `sources` maps an absolute path to source text, so a caller can supply
    modules that exist only in memory (an agent iterating on a candidate
    before anything touches the disk). A path present there is parsed from
    the string; everything else is read from the filesystem as usual.
    """

    def __init__(self, sources: dict[str, str] | None = None,
                 profile: AdmissionProfile | None = None) -> None:
        self._cache: dict[str, _LoadedModule] = {}
        self._stack: list[str] = []
        # Keys are normalised to abspath ONCE here. Every lookup below is by
        # abspath (`has_source`, `load`), so a relative key would never match
        # and the loader would fall through to `parse_file` on the SAME relative
        # path — silently admitting whatever sits on disk under that name in
        # place of the text the caller submitted (`gate_service.admit` passed
        # its keys through verbatim). A caller's in-memory source now stands in
        # for the file it names however the key was spelled.
        self._sources = {os.path.abspath(k): v
                         for k, v in (sources or {}).items()}
        # roadmap 319: REVL_IMPORT_PATH + the revl stdlib, read fresh per
        # loader so a test's `monkeypatch.setenv` takes effect.
        self._search_path = _default_search_path()
        # item 396: the untrusted-author profile and the set of ROOT module
        # abspaths. The no-extern refusal and the body-file resolution/read skip
        # are ROOT-scoped (re-review F4): an imported (`use`d) module resolves
        # its body files normally, so a pre-granted module may declare externs.
        self._profile = profile
        self._root_paths: set[str] = set()
        # item 410: abspath -> the search-path ENTRY that resolved it, recorded
        # by `resolve_use` when a `use` did NOT resolve relative to its importer
        # (an install-origin module: a REVL_IMPORT_PATH entry or the stdlib
        # default). A ref declared by such a module jails to that one entry's
        # tree, never the composition's user tree. Absent -> user-origin.
        self._install_root_of: dict[str, str] = {}
        # roadmap 422 F7: every `use "stdlib/..."` in this compile that did NOT
        # land on the stdlib this compiler ships, as
        # `{"written", "resolved", "origin"}`. See `stdlib_shadow`.
        self.stdlib_shadow: list[dict] = []

    def mark_roots(self, abs_paths) -> None:
        """Record the composition's root module abspaths BEFORE any load, so a
        root that is also reached as another root's `use` dependency is still
        recognised as a root when it is loaded (item 396 ordering)."""
        self._root_paths.update(abs_paths)

    def _root_dirs(self) -> list[str]:
        """The directory trees a `ref` (item 396 option B) may resolve inside:
        the directories of the composition's root compile files. The deploy-time
        import root (the py driver's appended `sys.path` entry) is derived the
        same way, so a compiled ref specifier and the driver agree."""
        return sorted({os.path.dirname(p) for p in self._root_paths})

    def _origin_install_root(self, abs_path: str) -> str | None:
        """item 410: the INSTALL ROOT a module's refs jail to, or `None` for a
        user-origin module (refs jail to `_root_dirs()`, unchanged 396(B)).

        Install-origin two ways, mirroring the design's origin classification:

        1. the module resolved through the item-319 search path (recorded by
           `resolve_use`) — its install root is that resolving ENTRY, so a
           REVL_IMPORT_PATH stand-in jails to its own entry, the stdlib default
           to `stdlib_root().parent`;
        2. its realpath is contained in `stdlib_root()` — the containment arm, so
           a stdlib module reached RELATIVELY by a sibling (relative resolution
           is primary and wins, never recorded in (1)) still classifies with its
           importer. Its install root is `stdlib_root().parent`.

        Origin DECIDES; containment of the ref TARGET never does. A user root
        file that happens to sit inside the revl checkout is still user-origin
        unless it lands in `stdlib_root()` itself.
        """
        recorded = self._install_root_of.get(abs_path)
        if recorded is not None:
            return recorded
        if _contained(os.path.realpath(abs_path),
                      os.path.realpath(str(stdlib_root()))):
            return str(stdlib_root().parent)
        return None

    def has_source(self, path: str) -> bool:
        return os.path.abspath(path) in self._sources

    def _exists(self, path: str) -> bool:
        return self.has_source(path) or os.path.exists(path)

    def resolve_use(self, importer_dir: str, importer_path: str, use: _ast.UseDecl,
                    *, in_memory: bool = False) -> str:
        """Resolve a `use` path to the file it names.

        Relative-to-the-importing-file resolution is primary and unchanged:
        it is tried first and, if it exists, wins outright — a search-path
        entry never shadows a genuine local file of the same relative path
        (roadmap 319). Only when nothing sits there does the search path
        (REVL_IMPORT_PATH, then the revl stdlib) get a turn, in order.

        `in_memory` says the IMPORTER was supplied as source text rather than
        read from disk. Under an untrusted-author profile that is the admitted
        source itself (or a module handed in beside it), and its imports are
        confined BEFORE anything is stat'd — see `_confine_use`.
        """
        primary = os.path.join(importer_dir, use.path)
        if in_memory and self._profile is not None and self._profile.untrusted:
            self._confine_use(importer_path, use, primary)
        if self._exists(primary):
            self._note_stdlib_shadow(use.path, primary, "importer-relative")
            return primary
        for base in self._search_path:
            candidate = os.path.join(base, use.path)
            if self._exists(candidate):
                # item 410: this module resolved through the search path (a
                # REVL_IMPORT_PATH entry or the stdlib default), so it is
                # install-origin and any ref it declares jails to THIS entry's
                # tree. Record the resolving entry keyed by the module's abspath.
                #
                # `use.path` is unvalidated (the parser only rejects empty), so a
                # `..`-escaping path can join this base yet resolve OUTSIDE it.
                # Only record install-origin when the resolved file's realpath is
                # actually CONTAINED in the matching base's realpath (compare both
                # sides: the default search path does not realpath its entries).
                # An escaping `use` falls through to user-origin, so its refs jail
                # to the user root and an install-tree ref is refused by 396(B) —
                # it can no longer forge stdlib-origin.
                if _contained(os.path.realpath(candidate),
                              os.path.realpath(base)):
                    self._install_root_of.setdefault(os.path.abspath(candidate), base)
                self._note_stdlib_shadow(use.path, candidate, f"search path {base}")
                return candidate
        hint = "`use` resolves paths relative to the importing file"
        if self._search_path:
            searched = ", ".join(self._search_path)
            hint += (f", then a search path ({searched}) — `{use.path}` was not "
                     "found relative to the importer or anywhere on that path")
        raise RevlError(importer_path, use.line,
                        f"cannot find imported module `{use.path}`", hint=hint)

    def _confine_use(self, importer_path: str, use: _ast.UseDecl,
                     primary: str) -> None:
        """Admission confinement of a `use` written by an untrusted author.

        The compile FOLLOWS a `use` path: `resolve_use` joins it to the
        importing directory and the loader parses whatever is there. For source
        whose author is untrusted that made every `use "<path>"` a probe of the
        admitting process's filesystem: the refusal that came back (a parse
        error at `<path>:1` naming the first token, or "cannot find imported
        module") disclosed whether the path exists and what it starts with. The
        MCP transport closed this at its dispatch layer (roadmap 425 F2) for
        source that arrived over the wire, but the library doors admit the same
        source without a transport — `Session.admit` (item 330, and the
        in-language `admit` crossing behind it), `Gate.propose` (item 334) — and
        each compiled with the profile and no jail. The door is the compile
        itself, so the confinement lives here, keyed on the ONE fact every door
        already states: `profile.untrusted`.

        Scope is the same premise the transport jail runs on. An in-memory
        importer under an untrusted profile is agent-authored (the admitted
        root, or a `modules=` entry supplied beside it); a `.rvl` read from
        disk was put there by a human, and its own `../lib/x.rvl` is the
        operator's composition layout, untouched. Two shapes escape and are
        refused: an absolute path, and a relative path that traverses upward
        out of the admitting directory. A relative path that stays inside
        resolves as before (the admitting directory is the operator's, the
        same sanction the transport gives its cwd), as does the search-path
        spelling of an installed module. A path the caller also supplies
        in-memory is exempt: it resolves out of the sources map and no
        filesystem is involved.

        Order is the point: this runs before `_exists`, so the refusal is
        byte-identical whether or not the named file exists. A confinement
        that leaked existence through its own message would be the defect it
        closes, in a smaller font (the 396 F4 no-oracle discipline)."""
        if not escaping_use_path(use.path) or self.has_source(primary):
            return
        raise RevlError(
            importer_path, use.line,
            f"admission confinement: `use \"{use.path}\"` in untrusted-authored "
            f"source may not name an absolute path or traverse upward out of "
            f"the admitting directory",
            hint="the compile follows a `use` path and reads the file it names, "
                 "so an unconfined one would report whether any path on the "
                 "machine exists and what its first token is. Supply the "
                 "imported module in-memory (`modules=`), keyed by the path the "
                 "import names; write the import relative to the admitting "
                 "directory; or, for an installed module, use its search-path "
                 "spelling (`use \"stdlib/str.rvl\"`). A trusted author's file "
                 "on disk resolves its own imports unchanged",
            category="admission",
        )

    def _note_stdlib_shadow(self, written: str, resolved: str,
                            origin: str) -> None:
        """Record a `use "stdlib/..."` that resolved somewhere OTHER than the
        stdlib this compiler ships (roadmap 422 F7).

        `use "stdlib/fs.rvl"` is not identity-pinned: relative-to-the-importer
        resolution is primary and wins outright (item 319), and `REVL_IMPORT_PATH`
        is searched before `stdlib_root().parent`, so a `stdlib/` directory beside
        the importing file, or one entry on an env var, supplies the module.
        Both are DELIBERATE (vendoring a stdlib copy is supported, and item 389
        stamps and drift-checks one), so neither is refused here. What was
        unsound was the silence: reading `use "stdlib/fs.rvl"` and concluding
        "confined witnessed fs" drew a security conclusion from a spelling, and
        nothing in the compile said the spelling had been satisfied elsewhere.

        Recording it makes the identity a MEASURED fact of the compile instead:
        it rides on the IR as `stdlib_shadow` and `revl` prints it beside the
        open-holes report. Present only when a shadow actually happened, so
        every non-shadowing composition's IR and audit stay byte-identical.

        Note what this does and does not claim. It says the `stdlib/` module
        that was compiled is not the one this compiler ships; it does NOT say
        the substitute is hostile, and a vendored copy is the common honest
        case. The drift check (item 389, `revl doctor`) is the other half and
        answers a different question: which VERSION, not which FILE."""
        if not written.startswith("stdlib/"):
            return
        real = os.path.realpath(resolved)
        if _contained(real, os.path.realpath(str(stdlib_root()))):
            return
        entry = {"written": written, "resolved": real, "origin": origin}
        if entry not in self.stdlib_shadow:
            self.stdlib_shadow.append(entry)

    def load(self, path: str) -> _LoadedModule:
        abs_path = os.path.abspath(path)
        if abs_path in self._stack:
            start = self._stack.index(abs_path)
            cycle = self._stack[start:] + [abs_path]
            rendered = " -> ".join(os.path.relpath(p) for p in cycle)
            raise RevlError(
                abs_path,
                1,
                f"import cycle: {rendered}",
                hint="module imports form a compile-time DAG; component dependencies are the "
                     "runtime graph checked separately by G3",
            )
        if abs_path in self._cache:
            return self._cache[abs_path]

        self._stack.append(abs_path)
        try:
            virtual = self._sources.get(abs_path)
            program = (Parser(virtual, abs_path).parse() if virtual is not None
                       else parse_file(abs_path))
            # item 396: for a ROOT module under a no-extern profile, refuse
            # BEFORE any body file is resolved, read, or stat'd, so the refusal
            # is byte-identical whether or not the named file exists (no
            # existence oracle, re-review F4). Root-scoped: an imported module
            # is a pre-granted dependency and may declare externs. A root that
            # IMPORTS a pre-granted module's host extern and reaches it directly
            # is the separate import/reach bypass, refused by
            # `check_no_host_extern_reach` in compile_files (item 330).
            is_root = abs_path in self._root_paths
            if (is_root and self._profile is not None
                    and self._profile.no_extern):
                _check_no_extern([program], self._profile)
            # item 396: resolve external host-body files under the jail,
            # replacing each HostBodyFile node with a spliced HostBody. A
            # virtual (in-memory) module resolves ONLY through the sources map
            # (no disk fallback, re-review F5).
            _resolve_body_files(program, os.path.dirname(abs_path),
                                self._sources, virtual is not None)
            # item 396 option B: resolve external host-MODULE refs against the
            # ROOT-tree jail (a different root than A's per-module jail: B's file
            # must be reachable at deploy time through one import root). A ref
            # resolving outside every root tree is refused; an imported module
            # inside the tree resolves normally. The pinned rel-path + hash flow
            # through lower into the additive `refs` IR key.
            # item 410: origin decides the ref jail's root set. An install-origin
            # module (search-path-resolved, or contained in stdlib_root()) jails
            # its refs to ONE install entry and stamps `root_kind = "stdlib"`; a
            # user module jails to the composition's root trees, byte-identical
            # to 396(B). A user ref never resolves against the install root and
            # vice versa — the loader passes the single set the origin selects.
            _resolve_refs(program, os.path.dirname(abs_path),
                          self._root_dirs(), self._sources, virtual is not None,
                          install_root=self._origin_install_root(abs_path))
            _check_user_py_body_imports(
                program, self._origin_install_root(abs_path) is not None
                or _contained(abs_path, os.path.realpath(str(stdlib_root()))))
            module = _LoadedModule(abs_path, os.path.dirname(abs_path), program)
            for fn in program.fn_decls:
                if fn.public:
                    module.public_fns[fn.name] = fn
            for type_decl in program.type_decls:
                if type_decl.public:
                    module.public_types[type_decl.name] = type_decl
            for extern in program.externs:
                if extern.public:
                    module.public_externs[extern.name] = extern
            for svc in program.services:
                module.services[svc.name] = svc

            self._cache[abs_path] = module
            for use in program.uses:
                dep_path = self.resolve_use(module.dir, abs_path, use,
                                            in_memory=virtual is not None)
                used = self.load(dep_path)
                if use.names is not None:
                    for name in use.names:
                        self._import_named(module, used, name, use.line)
                else:
                    if use.alias in module.aliases:
                        raise RevlError(abs_path, use.line,
                                        f"duplicate module alias `{use.alias}`")
                    module.aliases[use.alias] = used
                    module.pure_dependencies.add(id(used))
            self._stack.pop()
            return module
        except Exception:
            self._stack.pop()
            raise

    def _import_named(self, importer: _LoadedModule, used: _LoadedModule,
                      name: str, line: int) -> None:
        if name in used.public_fns:
            importer.named_fns.add(name)
            importer.pure_dependencies.add(id(used))
            return
        if name in used.public_types:
            importer.named_types.add(name)
            importer.pure_dependencies.add(id(used))
            return
        if name in used.public_externs:
            importer.named_externs.add(name)
            importer.pure_dependencies.add(id(used))
            return
        if name in used.services:
            importer.named_services.add(name)
            return

        where = os.path.relpath(used.path)
        if any(fn.name == name and not fn.public for fn in used.program.fn_decls):
            raise RevlError(importer.path, line,
                            f"`{name}` is module-private in `{where}` and cannot be imported (G1)",
                            hint=f"mark `fn {name}` as `pub fn {name}` in {where}")
        if any(td.name == name and not td.public for td in used.program.type_decls):
            raise RevlError(importer.path, line,
                            f"`{name}` is module-private in `{where}` and cannot be imported (G1)",
                            hint=f"mark `type {name}` as `pub type {name}` in {where}")
        if any(ext.name == name and not ext.public for ext in used.program.externs):
            raise RevlError(importer.path, line,
                            f"`{name}` is module-private in `{where}` and cannot be imported (G1)",
                            hint=f"mark `extern {name}` as `pub extern` in {where}")
        raise RevlError(importer.path, line,
                        f"`{name}` is not a public declaration in `{where}`")


def compile_source(source: str, filename: str = "<string>",
                   manifest: dict | None = None,
                   replacing: tuple[str, ...] = (),
                   modules: dict[str, str] | None = None,
                   profile: AdmissionProfile | None = None) -> dict:
    """Compile source text. Nothing is read from or written to the disk.

    `manifest` is the runtime-admission gate (see compile_files); `modules`
    supplies in-memory sources for `use` imports, keyed by the path the
    import names. Together they let a caller — an AI agent iterating on a
    candidate component, typically — check, admit and load code that has
    never existed as a file.

    `profile` (roadmap item 329) is the untrusted-author admission profile:
    when the AUTHOR of `source` is untrusted (the lighthouse code-mode
    direction), it forbids new `extern`/host-block declarations and bounds the
    services the turn may reach to an explicit granted set. A refusal is a
    compile error, not a runtime check. `None` = trusted author, unchanged.
    """
    if manifest is None and not modules:
        program = Parser(source, filename).parse()
        if program.uses:
            use = program.uses[0]
            raise RevlError(filename, use.line,
                            "`use` declarations need `modules=` (in-memory sources) or "
                            "compile_files with a real source path",
                            hint="a bare source string has no module directory from which "
                                 "to resolve the import")
        # no-extern refusal first (structural, no IO), so an untrusted author's
        # refusal is unchanged and precedes any body-file concern (item 396).
        _enforce_source([program], profile)
        _check_user_py_body_imports(program, False)
        # item 396: a bare in-memory source has no module directory and no
        # sources map, so a body file cannot resolve without opening disk, which
        # the `compile_source` contract forbids. Refuse structurally (no IO),
        # mirroring the `use` refusal above (re-review F5).
        if _program_has_body_file(program):
            line = next(
                body.line for ext in program.externs for body in ext.bodies
                if isinstance(body, _ast.HostBodyFile))
            raise RevlError(
                filename, line,
                "an extern host-body file (`= @backend file \"path\"`) needs "
                "`modules=` (in-memory sources) or `compile_files` with a real "
                "source path",
                hint="a bare source string has no module directory from which to "
                     "resolve the file, and `compile_source` reads nothing from "
                     "disk (item 396)")
        # item 396 option B: a ref needs a root tree to jail against and a
        # module directory to resolve relative to; a bare source string has
        # neither. Refuse structurally (no IO), mirroring the body-file refusal.
        if _program_has_ref(program):
            line = next(
                body.line for ext in program.externs for body in ext.bodies
                if isinstance(body, _ast.HostRef))
            raise RevlError(
                filename, line,
                "an extern host-module ref (`= @backend ref sym from \"path\"`) "
                "needs `modules=` (in-memory sources) or `compile_files` with a "
                "real source path",
                hint="a bare source string has no root compile tree to jail the "
                     "ref against, and `compile_source` reads nothing from disk "
                     "(item 396 option B)")
        document = check_and_lower(
            program, taint_strict=bool(profile and profile.taint_strict),
            untrusted=bool(profile and profile.untrusted))
        _enforce_document(document, profile)
        return document

    virtual = {os.path.abspath(filename): source}
    for path, text in (modules or {}).items():
        virtual[os.path.abspath(path)] = text
    return compile_files([filename], manifest=manifest, replacing=replacing,
                         sources=virtual, profile=profile)


def compile_files(paths: list[str], manifest: dict | None = None,
                  replacing: tuple[str, ...] = (),
                  sources: dict[str, str] | None = None,
                  profile: AdmissionProfile | None = None) -> dict:
    """Compile a composition: all services and components across the files
    are checked and linked together (the composition manifest, DESIGN §4).

    `manifest` — the runtime-admission gate: pass a previously compiled IR
    document (or its `manifest` plus `services`) and the new files are
    linked *against the running composition*: ambient services are in scope
    without redeclaration, and G2/G3 span both. A compiled component whose
    name matches a running one implicitly replaces it (the hot-swap case);
    `replacing` names additional components being withdrawn in the same
    admission (renames).

    The returned document's `components` are only the newly compiled ones;
    its `manifest` describes the whole resulting composition.
    """
    loader = _ModuleLoader(sources, profile)
    # item 396: mark every root abspath before loading, so the root-scoped
    # no-extern check and body-file resolution skip apply even to a root that is
    # reached as another root's `use` dependency.
    loader.mark_roots(os.path.abspath(p) for p in paths)
    root_modules = [_load_root(loader, path) for path in paths]

    merged = Program(filename=paths[0] if paths else "<none>")
    seen_services: dict[str, str] = {}
    seen_components: dict[str, str] = {}
    # A loaded module can appear more than once in the root list (or be reached
    # both as a root and through an import), so declarations need deduplication.
    # Do not use id(decl) here: object addresses are recycled after collection and
    # can make an unrelated declaration disappear from the merged program.
    emitted_keys: set[tuple[str, str, int]] = set()

    def declaration_key(module: _LoadedModule, kind: str, index: int) -> tuple[str, str, int]:
        return (module.path, kind, index)

    # Components and services from the root modules are the composition.
    # Components are never imported; services are pub by default.
    for module in root_modules:
        for index, svc in enumerate(module.program.services):
            if svc.name in seen_services:
                raise RevlError(module.path, svc.line,
                                f"duplicate service `{svc.name}` (also declared in {seen_services[svc.name]})")
            seen_services[svc.name] = module.path
            merged.services.append(svc)
            emitted_keys.add(declaration_key(module, "service", index))
        for index, comp in enumerate(module.program.components):
            if comp.name in seen_components:
                raise RevlError(module.path, comp.line,
                                f"duplicate component `{comp.name}` (also declared in {seen_components[comp.name]})")
            seen_components[comp.name] = module.path
            merged.components.append(comp)
            emitted_keys.add(declaration_key(module, "component", index))
        # fault tests name a component, and components are never imported, so
        # they ride with the composition's own modules rather than with the
        # pure-declaration closure that carries plain `test` blocks
        for index, fault in enumerate(module.program.fault_tests):
            if declaration_key(module, "fault", index) not in emitted_keys:
                merged.fault_tests.append(fault)
                emitted_keys.add(declaration_key(module, "fault", index))
        # item 256: a bound secret is a composition-scoped declaration, like a
        # component (never imported: it binds against a capability the roots
        # serve). Carry it into the merged program so `_lower_secrets` cross-
        # indexes it against the merged externs on the multi-file / CLI path,
        # exactly as it already does on the single-source `compile_source` path.
        for index, sec in enumerate(getattr(module.program, "secrets", None) or []):
            if declaration_key(module, "secret", index) not in emitted_keys:
                merged.secrets.append(sec)
                emitted_keys.add(declaration_key(module, "secret", index))

    # Directly imported services enter the composition service table. Alias
    # imports do not: a service is referred to by its interface name, not a
    # module-qualified path.
    for module in root_modules:
        for use in module.program.uses:
            used = loader.load(loader.resolve_use(module.dir, module.path, use))
            if use.names is not None:
                for name in use.names:
                    svc = used.services.get(name)
                    if svc is None:
                        continue
                    service_index = used.program.services.index(svc)
                    key = declaration_key(used, "service", service_index)
                    if key not in emitted_keys:
                        if svc.name in seen_services:
                            raise RevlError(module.path, use.line,
                                            f"duplicate service `{svc.name}` (also declared in {seen_services[svc.name]})")
                        seen_services[svc.name] = used.path
                        merged.services.append(svc)
                        emitted_keys.add(key)

    # Pure declarations emitted into the IR: every root module plus the
    # transitive closure of modules whose pure declarations are imported.
    # Private helpers of an imported module are emitted too (its functions
    # may call them), but fn_scopes below keeps them module-private.
    by_id = {id(module): module for module in loader._cache.values()}
    included: list[_LoadedModule] = list(root_modules)
    included_ids: set[int] = {id(m) for m in root_modules}
    queue = list(root_modules)
    while queue:
        module = queue.pop(0)
        # Sets of module ids are useful for membership, but their iteration order
        # depends on allocation and changes across otherwise identical compiles.
        for dep_id in sorted(module.pure_dependencies,
                             key=lambda value: by_id[value].path):
            if dep_id not in included_ids:
                included.append(by_id[dep_id])
                included_ids.add(dep_id)
                queue.append(by_id[dep_id])

    # CAPSTONE SEAM 2 (roadmap 228): a `use`d module's PRIVATE (non-`pub`)
    # top-level declarations must not enter the merged namespace. Before the
    # decls of the included modules are flattened into one program (below),
    # give every private fn/type/extern whose bare name is NOT unique across
    # the included set a per-module-qualified internal name, rewriting the
    # references to it *inside its own module*. Only `pub` names keep their
    # bare spelling, so two modules that each define a private `Ctx` (or
    # `contains`) co-compile, and a `use {dedent}`-then-local-`rstrip` no
    # longer collides — while a genuine duplicate of a `pub` name (neither is
    # mangled) still refuses. See _apply_module_privacy.
    _apply_module_privacy(included)

    for module in included:
        for index, decl in enumerate(module.program.type_decls):
            if declaration_key(module, "type", index) not in emitted_keys:
                merged.type_decls.append(decl)
                emitted_keys.add(declaration_key(module, "type", index))
        for index, decl in enumerate(module.program.fn_decls):
            if declaration_key(module, "fn", index) not in emitted_keys:
                merged.fn_decls.append(decl)
                emitted_keys.add(declaration_key(module, "fn", index))
        for index, decl in enumerate(module.program.externs):
            if declaration_key(module, "extern", index) not in emitted_keys:
                merged.externs.append(decl)
                emitted_keys.add(declaration_key(module, "extern", index))
        for index, decl in enumerate(module.program.tests):
            if declaration_key(module, "test", index) not in emitted_keys:
                merged.tests.append(decl)
                emitted_keys.add(declaration_key(module, "test", index))
        # prop tests are pure-declaration tests like plain `test` blocks: they
        # ride with the same closure (roadmap item 37)
        for index, decl in enumerate(module.program.prop_tests):
            if declaration_key(module, "prop_test", index) not in emitted_keys:
                merged.prop_tests.append(decl)
                emitted_keys.add(declaration_key(module, "prop_test", index))

    # Build checker scopes for every emitted function so a module-private
    # declaration from another module is not accidentally callable.
    for module in included:
        _check_user_py_body_imports(
            module.program,
            loader._origin_install_root(module.path) is not None
            or _contained(module.path, os.path.realpath(str(stdlib_root()))))
    for module in included:
        own_fns = {fn.name for fn in module.program.fn_decls}
        callables = own_fns | set(module.named_fns)
        alias_fns = {
            alias: set(used.public_fns)
            for alias, used in module.aliases.items()
        }
        for fn in module.program.fn_decls:
            merged.fn_scopes[id(fn)] = callables
            merged.fn_alias_scopes[id(fn)] = alias_fns

    # Declaration provenance for why-traces: every module the loader touched,
    # so a trace hop into an imported fn names the file it was declared in.
    for module in loader._cache.values():
        merged.decl_files.update(module.program.decl_files)

    ambient = None
    if manifest is not None:
        running = manifest.get("manifest", manifest)
        dropped = set(replacing) | {comp.name for comp in merged.components}
        ambient = {
            "services": manifest.get("services") or {},
            "components": [
                entry for entry in (running.get("components") or [])
                if entry.get("name") not in dropped
            ],
            # key -> service, so the admission gate (§5) can tell which running
            # components consume/provide a redeclared service. The manifest's
            # `inject`/`provides` are bare key lists; the service each key
            # carries lives on the full IR document's `components`, whose
            # `provides` is a {key: service} map. When only a bare manifest was
            # supplied this stays empty and the gate stays conservative.
            "provision_services": _provision_services(manifest),
            # key -> {type, component}: the state hand-off each running provider
            # exports (roadmap item 53). Read off the full IR document's
            # `components` (whose `handoff` survives lowering), not the manifest
            # projection. A dropped provider's hand-off is exactly what a
            # replacement must accept, so dropped entries stay in — the gate
            # compares the replacement's *accepted* shape against them.
            "handoffs": _running_handoffs(manifest),
        }
    # roadmap item 329: the untrusted-author profile, no-extern half — refuse a
    # new extern/host-block in the admitted source BEFORE lowering it, scoped to
    # the root modules (the source actually being admitted, not the pre-granted
    # imported closure). Inert without a profile, so a trusted compile is
    # byte-identical.
    _enforce_source([m.program for m in root_modules], profile)
    # items 330 + 329/transitive: the import/reach bypass of the no-extern check.
    # 330 refused a root that `use`s a host extern and reaches it directly, but
    # `compile_files` merges the whole transitive `included` closure into one
    # program, so an untrusted turn reaches host code through a `pub fn` wrapper in
    # a NON-root module too. Feed the reach sweep every host-body extern in the
    # merged closure (not just the root's imports) and let it follow the transitive
    # call graph across `merged.fn_decls`; refused before lowering runs a body.
    if profile is not None and profile.no_extern:
        _check_no_host_extern_reach(
            [m.program for m in root_modules],
            merged.fn_decls,
            _included_host_externs(included), profile)
    document = check_and_lower(
        merged, ambient, taint_strict=bool(profile and profile.taint_strict),
        untrusted=bool(profile and profile.untrusted))
    # the allowlist half — refuse a reach outside the granted service set, on the
    # lowered document's resolved requires/provides.
    _enforce_document(document, profile)
    # roadmap 422 F7: which file each `use "stdlib/..."` actually got. ADDITIVE
    # and present only when one of them did not come from this compiler's own
    # stdlib, so a composition that imports the shipped modules carries no new
    # IR key and every existing audit is byte-identical.
    if loader.stdlib_shadow:
        document["stdlib_shadow"] = loader.stdlib_shadow
    if manifest is not None:
        # The admission gate. Compiling a draft is fine — that is how an
        # agent gets a verdict on the parts it has written — but admitting
        # one into a running composition is not: a hole has a type and no
        # implementation (docs/holes.md).
        refuse_admission(document)
    return document


def _extern_signature(ext) -> str:
    """A compact `(P, Q) -> R` rendering of an extern's signature, for the
    duplicate-extern collision message (roadmap 393)."""
    params = ", ".join(p.type or "?" for p in ext.params)
    return f"({params}) -> {ext.returns or '()'}"


def _reject_clashing_private_externs(included: list[_LoadedModule]) -> None:
    """Refuse two modules that each privately declare the same extern name with
    DIFFERENT signatures (roadmap 393, revl-harness F-H39.2).

    Unlike a private fn — which carries its own body and so may be renamed apart
    invisibly — an extern names a host requirement by its bare name. Two modules
    privately declaring `si_now_ms` with `() -> Str` and `() -> Int` is a real
    collision; the private-namespace rename would only hide it and let a caller
    of the bare name fall through to the misleading `is not a declared
    requirement of <Component>` G1 error. Report it here as what it is.

    Identical-signature private externs stay renamed-apart (harmless, and this
    keeps the pass a no-op for every composition that compiles today).
    """
    by_name: dict[str, list[tuple[_LoadedModule, object]]] = {}
    for module in included:
        for ext in module.program.externs:
            if not ext.public:
                by_name.setdefault(ext.name, []).append((module, ext))

    for name, decls in by_name.items():
        if len({id(m) for m, _ in decls}) < 2:
            continue  # a single module re-declaring is caught as `duplicate extern`
        # name the first pair whose signatures actually differ.
        for i in range(len(decls)):
            for j in range(i + 1, len(decls)):
                m_a, ext_a = decls[i]
                m_b, ext_b = decls[j]
                if id(m_a) == id(m_b):
                    continue
                sig_a = _extern_signature(ext_a)
                sig_b = _extern_signature(ext_b)
                if sig_a == sig_b:
                    continue
                path_a = os.path.abspath(m_a.path)
                path_b = os.path.abspath(m_b.path)
                raise RevlError(
                    path_a, ext_a.line,
                    f"duplicate extern `{name}` declared in {path_a} and "
                    f"{path_b} (signatures differ: `{sig_a}` vs `{sig_b}`)",
                    hint="an extern names a host requirement by its bare name, "
                         "so unlike a private `fn` it cannot be renamed apart "
                         "per module — give the two host functions distinct "
                         "names, or `pub` one and `use` it from the other module "
                         "so a single declaration is shared",
                )


def _apply_module_privacy(included: list[_LoadedModule]) -> None:
    """Namespace every module-private top-level declaration (roadmap 228).

    The merged program flattens the included modules into one flat table keyed
    by bare name (`_lower_fns`' duplicate check, `_lower_type_decls`,
    `_signature_table`). A private helper of module A therefore both false-
    collides with — and silently overwrites — a same-named private of module B.
    Public names are the module's *interface* and must stay bare (they are what
    `use { … }` imports and what backends emit by contract); private names are
    the module's *implementation* and are referenced only from within the same
    module, so they can be renamed with no visible effect.

    A private decl is renamed only when its bare name is NOT unique across the
    included set — the sole situation that produces a collision. This makes the
    pass a no-op for every composition that compiles today (they have no cross-
    module private-name clash, or they would already error), so it introduces
    no churn: it strictly *adds* the ability for clashing privates to coexist.

    Functions and externs share one signature table, so they share a namespace
    for this purpose; types are a separate namespace. `pub` duplicates stay a
    hard error because neither side is a private and so neither is renamed.
    """
    # roadmap 393 (revl-harness F-H39.2): a clashing private EXTERN is not a
    # genuinely-local helper the way a private fn is. A private fn has its own
    # body in each module, so renaming the two apart is invisible; an extern has
    # no body of its own — it names a HOST requirement by its bare name. Two
    # modules privately declaring the same extern name with DIFFERENT signatures
    # is therefore a real collision, and the rename below only papers over it:
    # it leaves any component/pure body that calls the bare `si_now_ms`
    # resolving to nothing, which surfaces downstream as the misleading
    # `si_now_ms is not a declared requirement of <Component>` (G1) error —
    # sending the author to add a `requires` clause for something that is not a
    # service. Catch the collision HERE, before the rename, and report it AS a
    # duplicate that names both declaring files and the signature mismatch.
    _reject_clashing_private_externs(included)

    val_owners: dict[str, set[int]] = {}   # fn/extern name -> {id(module)}
    type_owners: dict[str, set[int]] = {}   # type name      -> {id(module)}
    for module in included:
        for decl in list(module.program.fn_decls) + list(module.program.externs):
            val_owners.setdefault(decl.name, set()).add(id(module))
        for decl in module.program.type_decls:
            type_owners.setdefault(decl.name, set()).add(id(module))

    for idx, module in enumerate(included):
        program = module.program
        val_renames: dict[str, str] = {}
        type_renames: dict[str, str] = {}
        for decl in list(program.fn_decls) + list(program.externs):
            if not decl.public and len(val_owners.get(decl.name, ())) > 1:
                val_renames[decl.name] = f"{decl.name}__m{idx}"
        for decl in program.type_decls:
            if not decl.public and len(type_owners.get(decl.name, ())) > 1:
                type_renames[decl.name] = f"{decl.name}__m{idx}"
        if val_renames or type_renames:
            _rewrite_module(program, val_renames, type_renames)


def _subst_type(t: str | None, renames: dict[str, str]) -> str | None:
    """Head-substitute a type annotation, recursing through generics and
    function types exactly as lower.py's alias expansion does (so a renamed
    private type is rewritten inside `List[Ctx]`, `Opt[Ctx]`, `(Ctx) -> T`)."""
    if not t or not renames:
        return t
    head, args = parse_type(t)
    if args:
        return format_type(head, [_subst_type(a, renames) for a in args])
    return renames.get(head, head) if head is not None else head


def _rewrite_module(program: Program, val_renames: dict[str, str],
                    type_renames: dict[str, str]) -> None:
    """Rename the module's private decls and every reference to them that lives
    inside the module (its only referents — a private name never leaks)."""
    def subst(t: str | None) -> str | None:
        return _subst_type(t, type_renames)

    # 1. declaration sites: rename the decl itself, then rewrite the type
    #    annotations it carries (a NON-renamed decl may still mention a renamed
    #    private type, so every decl's annotations are swept, not just renamed
    #    ones).
    for fn in program.fn_decls:
        fn.name = val_renames.get(fn.name, fn.name)
        for p in fn.params:
            p.type = subst(p.type)
        fn.returns = subst(fn.returns)
    for ext in program.externs:
        ext.name = val_renames.get(ext.name, ext.name)
        for p in ext.params:
            p.type = subst(p.type)
        ext.returns = subst(ext.returns)
    for td in program.type_decls:
        td.name = type_renames.get(td.name, td.name)
        for fld in td.fields:
            fld.type = subst(fld.type)
        for case in td.cases:
            case.payload = subst(case.payload)

    # 2. bodies: value references (shadow-aware) + body-level type annotations.
    for fn in program.fn_decls:
        _rewrite_body(fn.body, val_renames, type_renames,
                      {p.name for p in fn.params})
    for test in program.tests:
        _rewrite_body(test.body, val_renames, type_renames, set())
    for ptest in program.prop_tests:
        _rewrite_body(ptest.body, val_renames, type_renames,
                      {p.name for p in ptest.params})
    # an extern's undo/compensate expressions can call renamed helpers
    for ext in program.externs:
        for slot in (getattr(ext, "undo", None), getattr(ext, "compensate", None)):
            if slot is not None:
                _rewrite_expr(slot, val_renames, type_renames, set())


def _rewrite_body(stmts: list, val_renames: dict[str, str],
                  type_renames: dict[str, str], bound: set[str]) -> None:
    """Rewrite a statement block in place. `bound` is the set of local names in
    scope; a bare reference that names a local is a shadow and is left alone.
    Bindings introduced by a statement are visible to the statements after it,
    so `bound` is threaded (and copied when a nested block opens its own scope).
    """
    bound = set(bound)
    for stmt in stmts:
        _rewrite_stmt(stmt, val_renames, type_renames, bound)


def _rewrite_stmt(stmt, val_renames, type_renames, bound: set[str]) -> None:
    def subst(t):
        return _subst_type(t, type_renames)

    if isinstance(stmt, _ast.LetStmt):
        stmt.type = subst(stmt.type)
        _rewrite_expr(stmt.value, val_renames, type_renames, bound)
        bound.add(stmt.name)
    elif isinstance(stmt, _ast.LetPatternStmt):
        _rewrite_expr(stmt.value, val_renames, type_renames, bound)
        pat = stmt.pattern
        if isinstance(pat, _ast.RecordPattern):
            bound.update(pat.fields)
        elif isinstance(pat, _ast.ListPattern):
            bound.update(pat.binds)
            if pat.rest:
                bound.add(pat.rest)
    elif isinstance(stmt, _ast.AssignStmt):
        _rewrite_expr(stmt.value, val_renames, type_renames, bound)
    elif isinstance(stmt, (_ast.ExprStmt, _ast.AssertStmt, _ast.FailStmt,
                           _ast.AwaitStmt)):
        _rewrite_expr(stmt.expr, val_renames, type_renames, bound)
    elif isinstance(stmt, _ast.ReturnStmt):
        if stmt.expr is not None:
            _rewrite_expr(stmt.expr, val_renames, type_renames, bound)
    elif isinstance(stmt, _ast.IfStmt):
        _rewrite_expr(stmt.cond, val_renames, type_renames, bound)
        _rewrite_body(stmt.then, val_renames, type_renames, bound)
        _rewrite_body(stmt.otherwise or [], val_renames, type_renames, bound)
    elif isinstance(stmt, _ast.WhileStmt):
        _rewrite_expr(stmt.cond, val_renames, type_renames, bound)
        _rewrite_body(stmt.body, val_renames, type_renames, bound)
    elif isinstance(stmt, _ast.ForStmt):
        _rewrite_expr(stmt.iterable, val_renames, type_renames, bound)
        inner = set(bound)
        inner.add(stmt.bind)
        _rewrite_body(stmt.body, val_renames, type_renames, inner)


def _rewrite_expr(expr, val_renames, type_renames, bound: set[str]) -> None:
    def recur(e, b=bound):
        _rewrite_expr(e, val_renames, type_renames, b)

    if isinstance(expr, _ast.ExprVar):
        if expr.name in val_renames and expr.name not in bound:
            expr.name = val_renames[expr.name]
    elif isinstance(expr, _ast.ExprBin):
        recur(expr.left)
        recur(expr.right)
    elif isinstance(expr, _ast.ExprUn):
        recur(expr.operand)
    elif isinstance(expr, _ast.ExprCall):
        recur(expr.callee)
        for arg in expr.args:
            recur(arg)
    elif isinstance(expr, (_ast.ExprField, _ast.ExprOptField)):
        recur(expr.target)
    elif isinstance(expr, _ast.ExprIndex):
        recur(expr.target)
        recur(expr.index)
    elif isinstance(expr, _ast.ExprOptCall):
        recur(expr.target)
        for arg in expr.args:
            recur(arg)
    elif isinstance(expr, _ast.ExprIf):
        recur(expr.cond)
        recur(expr.then)
        recur(expr.otherwise)
    elif isinstance(expr, _ast.ExprRecord):
        for _, value in expr.fields:
            recur(value)
    elif isinstance(expr, _ast.ExprRecordUpdate):
        recur(expr.base)
        for _, value in expr.updates:
            recur(value)
    elif isinstance(expr, _ast.ExprList):
        for item in expr.items:
            recur(item)
    elif isinstance(expr, _ast.ExprArrow):
        # the author's annotations; the module-merge rename runs before
        # checking, so the checker's resolved fields are still unset
        expr.written_param_types = [_subst_type(t, type_renames)
                                    for t in expr.written_param_types]
        expr.written_returns = _subst_type(expr.written_returns, type_renames)
        recur(expr.body, bound | set(expr.params))
    elif isinstance(expr, _ast.ExprMatch):
        recur(expr.scrutinee)
        for _, bind, body in expr.arms:
            arm = bound | ({bind} if bind is not None else set())
            recur(body, arm)
            if isinstance(body, _ast.ExprBlockArm):
                inner = set(arm)
                for s in body.stmts:
                    recur(s.value, inner)
                    inner.add(s.name)
                recur(body.tail, inner)
    elif isinstance(expr, _ast.ExprHole):
        expr.type = _subst_type(expr.type, type_renames)
    elif isinstance(expr, _ast.Interp):
        for kind, part in expr.parts:
            if kind == "expr":
                recur(part)


def _included_host_externs(included: list[_LoadedModule]) -> dict:
    """items 330 + 329/transitive: map every host-body extern NAME declared in ANY
    included module to `(decl, module_path)`.

    The 330 feeder collected only the ROOT's directly-imported extern surface
    (`use "..." { sh }` on a root). But `compile_files` merges and lowers the whole
    transitive `included` closure into one program, so a host body declared in a
    NON-root module (reached through a `pub fn` wrapper that a root imports) is
    just as callable and just as lowered. `check_no_extern` is root-scoped, so a
    non-root module may freely DECLARE host-body externs; the reach sweep must
    therefore see EVERY host body in the merged closure, not just the root's imports.

    An extern with a non-empty `bodies` list is verbatim host code (item 24, the
    gate does not sandbox it); a bodyless extern is a deploy-wired host requirement,
    not smuggled host code, so it is not collected. The name is the merged-program
    key `_iter_var_refs` sees: `_apply_module_privacy` has already run, so a private
    extern carries its (possibly mangled) name and a `pub` extern its bare name,
    consistent with the reach sweep's references."""
    result: dict = {}
    for module in included:
        for ext in module.program.externs:
            if ext.bodies:
                result.setdefault(ext.name, (ext, module.path))
    return result


def _load_root(loader: _ModuleLoader, path: str) -> _LoadedModule:
    # a virtual source stands in for the file it names (in-memory compilation)
    if not loader.has_source(path) and not os.path.exists(path):
        raise RevlError(path, 1, f"file not found: {path}")
    return loader.load(path)


def _provision_services(manifest: dict) -> dict[str, str]:
    """Map every provision key in the running document to the service it
    carries, for the admission gate's consumer/provider-relative drift check.

    The full IR document's `components` carry `provides` as a {key: service}
    map (unlike the manifest projection, whose `provides` is a bare key list).
    A caller that supplied only the manifest projection has no such map, so the
    result is empty and the gate treats every key as unresolved."""
    mapping: dict[str, str] = {}
    for comp in manifest.get("components") or []:
        provides = comp.get("provides")
        if isinstance(provides, dict):
            for key, service in provides.items():
                mapping[key] = service
    return mapping


def _running_handoffs(manifest: dict) -> dict[str, dict]:
    """The state hand-off each running provider *exports*, keyed by the
    provided key it hangs off (roadmap item 53).

    Read off the full IR document's `components`, whose `handoff` field
    (`{"key", "type"}`) survives lowering — the manifest projection drops it.
    A provider being *replaced* by this admission still contributes: its
    exported shape is exactly what the replacement's `accept` must be
    compatible with, so nothing is filtered here. A bare-manifest caller (no
    full `components`) yields an empty map and the hand-off gate is a no-op."""
    handoffs: dict[str, dict] = {}
    for comp in manifest.get("components") or []:
        h = comp.get("handoff")
        if isinstance(h, dict) and h.get("key"):
            handoffs[h["key"]] = {"type": h.get("type"),
                                  "component": comp.get("name")}
    return handoffs
