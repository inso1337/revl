"""Pipeline: source files -> parse -> check/lower -> link -> IR document."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .errors import RevlError
from .lower import check_and_lower
from .parser import ExternDecl, FnDecl, Parser, Program, ServiceDecl, TypeDecl, parse_file


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


class _ModuleLoader:
    """Loads modules and resolves `use` with cycle detection.

    `sources` maps an absolute path to source text, so a caller can supply
    modules that exist only in memory (an agent iterating on a candidate
    before anything touches the disk). A path present there is parsed from
    the string; everything else is read from the filesystem as usual.
    """

    def __init__(self, sources: dict[str, str] | None = None) -> None:
        self._cache: dict[str, _LoadedModule] = {}
        self._stack: list[str] = []
        self._sources = sources or {}

    def has_source(self, path: str) -> bool:
        return os.path.abspath(path) in self._sources

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
                dep_path = os.path.join(module.dir, use.path)
                # a virtual source stands in for the file it names
                if not self.has_source(dep_path) and not os.path.exists(dep_path):
                    raise RevlError(
                        abs_path, use.line,
                        f"cannot find imported module `{use.path}`",
                        hint="`use` resolves paths relative to the importing file",
                    )
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
                   modules: dict[str, str] | None = None) -> dict:
    """Compile source text. Nothing is read from or written to the disk.

    `manifest` is the runtime-admission gate (see compile_files); `modules`
    supplies in-memory sources for `use` imports, keyed by the path the
    import names. Together they let a caller — an AI agent iterating on a
    candidate component, typically — check, admit and load code that has
    never existed as a file.
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
        return check_and_lower(program)

    virtual = {os.path.abspath(filename): source}
    for path, text in (modules or {}).items():
        virtual[os.path.abspath(path)] = text
    return compile_files([filename], manifest=manifest, replacing=replacing,
                         sources=virtual)


def compile_files(paths: list[str], manifest: dict | None = None,
                  replacing: tuple[str, ...] = (),
                  sources: dict[str, str] | None = None) -> dict:
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
    loader = _ModuleLoader(sources)
    root_modules = [_load_root(loader, path) for path in paths]

    merged = Program(filename=paths[0] if paths else "<none>")
    seen_services: dict[str, str] = {}
    seen_components: dict[str, str] = {}
    emitted_ids: set[int] = set()

    # Components and services from the root modules are the composition.
    # Components are never imported; services are pub by default.
    for module in root_modules:
        for svc in module.program.services:
            if svc.name in seen_services:
                raise RevlError(module.path, svc.line,
                                f"duplicate service `{svc.name}` (also declared in {seen_services[svc.name]})")
            seen_services[svc.name] = module.path
            merged.services.append(svc)
            emitted_ids.add(id(svc))
        for comp in module.program.components:
            if comp.name in seen_components:
                raise RevlError(module.path, comp.line,
                                f"duplicate component `{comp.name}` (also declared in {seen_components[comp.name]})")
            seen_components[comp.name] = module.path
            merged.components.append(comp)
            emitted_ids.add(id(comp))

    # Directly imported services enter the composition service table. Alias
    # imports do not: a service is referred to by its interface name, not a
    # module-qualified path.
    for module in root_modules:
        for use in module.program.uses:
            used = loader.load(os.path.join(module.dir, use.path))
            if use.names is not None:
                for name in use.names:
                    svc = used.services.get(name)
                    if svc is not None and id(svc) not in emitted_ids:
                        if svc.name in seen_services:
                            raise RevlError(module.path, use.line,
                                            f"duplicate service `{svc.name}` (also declared in {seen_services[svc.name]})")
                        seen_services[svc.name] = used.path
                        merged.services.append(svc)
                        emitted_ids.add(id(svc))

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
        for dep_id in module.pure_dependencies:
            if dep_id not in included_ids:
                included.append(by_id[dep_id])
                included_ids.add(dep_id)
                queue.append(by_id[dep_id])

    for module in included:
        for decl in module.program.type_decls:
            if id(decl) not in emitted_ids:
                merged.type_decls.append(decl)
                emitted_ids.add(id(decl))
        for decl in module.program.fn_decls:
            if id(decl) not in emitted_ids:
                merged.fn_decls.append(decl)
                emitted_ids.add(id(decl))
        for decl in module.program.externs:
            if id(decl) not in emitted_ids:
                merged.externs.append(decl)
                emitted_ids.add(id(decl))
        for decl in module.program.tests:
            if id(decl) not in emitted_ids:
                merged.tests.append(decl)
                emitted_ids.add(id(decl))

    # Build checker scopes for every emitted function so a module-private
    # declaration from another module is not accidentally callable.
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
        }
    return check_and_lower(merged, ambient)


def _load_root(loader: _ModuleLoader, path: str) -> _LoadedModule:
    # a virtual source stands in for the file it names (in-memory compilation)
    if not loader.has_source(path) and not os.path.exists(path):
        raise RevlError(path, 1, f"file not found: {path}")
    return loader.load(path)
