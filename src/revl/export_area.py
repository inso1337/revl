"""Which files are author-written by their path (issues #2524, #2547).

`revl_export`, the one MCP verb that writes to disk, writes source only under
`<root>/.revl/export/`. So a file inside any `.revl/export/` directory is the
author's, by path, always: the MCP server compiles it under the authoring
profile (`mcp.server.compile_under_authoring`), and so does every other
compile through `CliRule`, the rule `compiler.compile_files` and
`compiler.compile_source` ask.

This module is the one place that decides whether a path lies inside an
export directory; the export jail (`mcp.export_jail`) and the compiler both
ask it. It lives outside `revl.mcp` so that compiling never imports the server
(issue #1780).

The rule decides by files. A file read from disk is asked about as named and
as it resolves through symbolic links, and so is text a caller holds in
memory under the name of a file that exists (one it read itself). The one
exception is an admission against a running composition, the hot path: its
in-memory text is asked about by the name it is compiled under, and only when
that name lies inside an export directory does the rule look at the disk, to
see that a file is there. Text compiled under a label that names no file (a virtual or
relative name, a path remembered from an earlier read, or text held only in
memory) compiles exactly as it did before the rule (issue #2563).
"""
from __future__ import annotations

import contextlib
import os
import sys
import unicodedata
from dataclasses import replace

EXPORT_DIR = os.path.join(".revl", "export")
_EXPORT_PARTS = tuple(EXPORT_DIR.split(os.sep))


def real(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def fold(text: str) -> str:
    """`text` NFC-normalised and case-folded: the spelling a filesystem that
    folds case or Unicode normalisation treats as the same name."""
    return unicodedata.normalize(
        "NFC", unicodedata.normalize("NFC", text).casefold())


def under_export_dir(path: str) -> bool:
    """Whether the spelling `path` has a `.revl/export` directory among its
    parts, compared case-folded. Reads nothing from the disk."""
    parts = [fold(part) for part in path.split(os.sep)]
    width = len(_EXPORT_PARTS)
    return any(tuple(parts[i:i + width]) == _EXPORT_PARTS
               for i in range(len(parts) - width + 1))


def named_in_export_area(path: str) -> bool:
    """Whether `path` as named (absolute, `..` folded away) lies inside an
    export directory. Reads nothing from the disk."""
    return under_export_dir(os.path.normpath(os.path.abspath(path)))


def in_export_area(path: str) -> bool:
    """Whether `path` lies inside any `.revl/export` directory, whichever root
    it was written under: as named (absolute, `..` folded away) or as it
    resolves through symbolic links. Either one is enough, since counting a
    file as author-written only ever adds the profile. Compared
    case-insensitively: on a filesystem that folds case, `.REVL/Export` IS the
    export directory, and on one that does not, counting it as one only ever
    profiles more. The server and the command line both decide by this."""
    return named_in_export_area(path) or under_export_dir(real(path))


def read_exact(path: str) -> str | None:
    """The file's text with no newline translation: the bytes, decoded."""
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def exported_texts(paths) -> dict[str, str]:
    """`{abspath: text}` of every path in `paths` inside an export directory:
    the files whose text is the agent's."""
    out = {}
    for path in paths:
        if in_export_area(path):
            text = read_exact(path)
            if text is not None:
                out[os.path.abspath(path)] = text
    return out


def author_profile(base=None):
    """The authoring profile the MCP server compiles agent text under by
    default (`mcp.server.AuthoringTrust().profile()`): the untrusted-author
    profile with the reach allowlist left off, carrying the operator's realm
    bindings from `base`. Built here, not asked of the server, so compiling
    never imports it."""
    from .admit_profile import AdmissionProfile  # noqa: PLC0415

    bindings = tuple(getattr(base, "realm_bindings", ()) or ())
    return replace(AdmissionProfile.untrusted_author((), bindings),
                   granted=None)


class CliRule:
    """The compile side of the TRUST rule (issue #2547): a file under a
    `.revl/export/` directory is author-written whoever loads it, so a compile
    outside the MCP server takes it under the authoring profile the server
    uses by default, the same way the server's `compile_under_authoring`
    does: its text rides in memory with no operator text to diff against, and
    a pass that reads one from disk by `use` is repeated with it riding, until
    a pass reads none.

    One is in force for every compile in the process unless the MCP server
    turned it off (see `compiler.export_rule`); `revl.__main__.main` installs
    the one for the command it dispatches (see `cli_rule`). With `trust` (the
    command was given `--trust-exported`) the files compile as the operator's
    own and one line on stderr names them. `offer_flag` says whether the
    command takes that flag, so the note names it only where it works. The
    rule is never read from the environment or a configuration file."""

    def __init__(self, trust: bool = False, offer_flag: bool = False) -> None:
        self.trust = trust
        self.offer_flag = offer_flag
        self._said: set[str] = set()

    @staticmethod
    def exported(path: str) -> bool:
        """Whether `path`, a file the compile reads from disk, is
        author-written: it exists and lies inside an export directory, as
        named or as it resolves. A `<...>` name is a placeholder, never a
        file."""
        return (not os.path.basename(path).startswith("<")
                and os.path.isfile(path) and in_export_area(path))

    @staticmethod
    def exported_text(path: str) -> bool:
        """The admission hot path's question about text held in memory and
        compiled under the name `path` against a running composition: the
        name lies inside an export directory and a file is there. A name
        outside one is decided without touching the disk (no `stat`, issue
        #2195), and a name with no file behind it compiles as before. Every
        other compile of in-memory text asks `exported`, which also follows
        the name through symbolic links."""
        return (not os.path.basename(path).startswith("<")
                and named_in_export_area(path) and os.path.isfile(path))

    @staticmethod
    def author_profile(base=None):
        return author_profile(base)

    def applies(self, profile) -> bool:
        """Whether a compile under `profile` is one this rule decides: an
        operator compile (no profile, or one that does not distrust the
        author). A compile already under an untrusted author's profile keeps
        its own."""
        return profile is None or not profile.untrusted

    def _say(self, paths, text: str) -> None:
        fresh = sorted({os.path.abspath(p) for p in paths} - self._said)
        if fresh:
            self._said.update(fresh)
            print(text.format(", ".join(fresh)), file=sys.stderr)

    def source_profile(self, filename: str, profile):
        """The profile `compile_source` uses for text labelled `filename` when
        nothing else is read: the author's when `filename` names a file on
        disk under the export directory, as named or as it resolves (a
        caller that read the file itself, such as `revl adapt`, passes the
        name it read it by), else `profile` unchanged."""
        if not self.exported(filename):
            return profile
        if self.trust:
            self._say([filename], TRUSTED_LINE)
            return profile
        self._say([filename], self._profiled_line())
        return self.author_profile(profile)

    def _profiled_line(self) -> str:
        return PROFILED_LINE + (PROFILED_FLAG if self.offer_flag
                                else PROFILED_COPY)

    def compile_files(self, compile_once, paths, sources, profile, profiles,
                      operator_sources, report, admitting: bool = False):
        """Run `compile_once(sources=, profile=, profiles=, operator_sources=,
        report=)` for a `compile_files` call under this rule. `admitting` is a
        compile against a running composition (the admission hot path): its
        in-memory text is asked about by name first (`exported_text`). Any
        other in-memory text whose name is a file, such as text a caller read
        from that file itself, is asked about as named and as it resolves."""
        sources = {os.path.abspath(k): v for k, v in (sources or {}).items()}
        held = self.exported_text if admitting else self.exported

        def is_exported(path: str) -> bool:
            if os.path.abspath(path) in sources:
                return held(path)
            return self.exported(path)

        if self.trust:
            # named before the compile, so one that raises still says what it
            # trusted; files reached by `use` are named once it has read them
            self._say([p for p in list(paths) + list(sources)
                       if is_exported(p)], TRUSTED_LINE)
            seen: dict = {}
            document = compile_once(sources=sources or None, profile=profile,
                                    profiles=profiles,
                                    operator_sources=operator_sources,
                                    report=seen)
            self._say([p for p in seen.get("disk_reads") or ()
                       if self.exported(p)], TRUSTED_LINE)
            if report is not None:
                report.update(seen)
            return document
        authored = {p: t for p, t in sources.items() if held(p)}
        authored.update({p: t for p, t in exported_texts(
            [p for p in paths
             if os.path.abspath(p) not in sources and self.exported(p)]).items()
            if p not in authored})
        # what the caller supplied in memory and is not exported is the
        # operator's own text, so the diff rule does not count it as authored
        operator = {p: t for p, t in sources.items() if p not in authored}
        operator.update(operator_sources or {})
        while True:
            seen = {}
            if authored:
                self._say(authored, self._profiled_line())
                author = self.author_profile(profile)
                document = compile_once(
                    sources={**sources, **authored}, profile=author,
                    profiles=_profile_exported(profiles, author, is_exported),
                    operator_sources=operator, report=seen)
            else:
                document = compile_once(
                    sources=sources or None, profile=profile,
                    profiles=profiles, operator_sources=operator_sources,
                    report=seen)
            pending = {p: t for p, t in (seen.get("disk_reads") or {}).items()
                       if p not in authored and self.exported(p)}
            if not pending:
                break
            authored.update(pending)
        if report is not None:
            report.update(seen)
            roots = {os.path.abspath(p) for p in paths}
            riding = {p: t for p, t in authored.items()
                      if p not in roots and p not in sources}
            if riding:
                report["dependencies"] = {
                    **(report.get("dependencies") or {}), **riding}
        return document


PROFILED_LINE = ("note: {} lies under .revl/export/, so it is author-written and "
                 "compiles under the authoring profile")
# the end of the note: the flag where the command takes it, else the one way
# that works everywhere (`truc` and library callers take no such flag)
PROFILED_FLAG = "; pass --trust-exported to compile it as your own"
PROFILED_COPY = ("; to compile it as your own, copy it out of the export "
                 "directory")
TRUSTED_LINE = "--trust-exported: compiled as the operator's own: {}"


def _profile_exported(profiles, author, is_exported):
    """`profiles` with every exported root that would compile as the
    operator's moved to the authoring profile."""
    if profiles is None:
        return None
    return {p: (author if is_exported(p)
                and (prof is None or not prof.untrusted) else prof)
            for p, prof in profiles.items()}


@contextlib.contextmanager
def cli_rule(trust_exported: bool = False, off: bool = False,
             offer_flag: bool = True):
    """Install the rule for one command (see `CliRule`): with
    `--trust-exported` when the command was given it, or `off` for `revl mcp
    serve`, whose server decides by its own path rule. `offer_flag` is whether
    the command takes `--trust-exported`. The rule is set for this context
    only and restored on the way out (`compiler.export_rule`)."""
    from . import compiler  # noqa: PLC0415

    rule = None if off else CliRule(trust=bool(trust_exported),
                                    offer_flag=offer_flag)
    with compiler.export_rule(rule):
        yield rule


def server_door(handle):
    """`handle` with the rule off for every request it answers: the MCP
    server compiles a file under `.revl/export/` by its own path rule and the
    operator's `--author-trust` (issue #2524), never by this one as well."""
    import functools  # noqa: PLC0415

    from . import compiler  # noqa: PLC0415

    @functools.wraps(handle)
    def answered(*args, **kwargs):
        with compiler.export_rule(None):
            return handle(*args, **kwargs)

    return answered
