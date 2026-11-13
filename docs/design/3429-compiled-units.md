# Per-file compiled units (issue #3429)

Status: design, with the first slice implemented behind an explicit opt-in.

## The question

`compile_files` reads and parses every file on every call, then checks,
lowers and links the whole program in one pass. Nothing is kept on disk, so a
composition where one file changed pays the same compile as one where every
file changed. The owner asked for the C model: keep a compiled unit per file
and rebuild only the units whose inputs changed.

## Measured

Plain wall-clock timers (no profiler) around the compile's phases, revl main
at the base of this change, the revl-harness default ("code") composition:
35 root files, 85 parsed modules. The machine was loaded and totals moved
between 11 s and 39 s across runs; the shares are stable. One run (17.5 s):

| phase | seconds | share |
|---|---|---|
| read the files | 0.02 | 0 % |
| parse (`parse_text`, 85 calls) | 3.88 | 22 % |
| loader resolution, merge, privacy and collision checks, admission | ~0.8 | 5 % |
| `_check_declared_type_arity` over the merged program | 1.63 | 9 % |
| `check_and_lower` | 11.16 | 64 % |
| of which `check_taint` | 1.61 | |
| of which `_resolve_type_aliases` | 1.55 | |
| of which `_lower_fns` | 1.17 | |
| of which `_refuse_callable_shadowing` | 1.10 | |
| of which ownership `annotate_ir` | 0.97 | |
| of which `_lower_tests` | 0.79 | |
| of which `_lower_component` (39 calls) | 0.45 | |
| of which `_emitting_capabilities`, `_async_callables` | 0.88 | |
| of which the remaining steps (`_link` is under 0.01 s) | ~2.6 | |

## The unit

Today the compiler has one natural per-file boundary: the module loader. Each
file is read once, parsed once (`parse_text`), then resolved in place (body
files, host-module refs, assets) and wired to its imports. After the loader,
`_compile_files` MERGES every included module into one `Program` whose nodes
are the modules' own nodes, and every later phase runs on that merged program.
Nothing after the merge is organised by file: the type table, the function
table and the component list are global, and most checks key their tables by
name across the whole program.

So the unit of the first slice is the parse of one file: the `Program` that
`Parser(text, path).parse()` returns, before provenance is stamped.

## Classification

| phase | depends on | class |
|---|---|---|
| read | the file | per file |
| parse | the file's text, the path it is parsed as, the compiler | per file (kept in slice 1) |
| provenance (`record_provenance`) | the path relative to the working directory, object identity | per file, recomputed on every load (identity cannot be stored) |
| body-file, ref and asset resolution | the file plus the bytes of the files it names, the root set of the composition, the install origin | per file given other files and the root set (not kept: cheap, and its inputs are files the key would have to list) |
| no-extern / no-asset refusal | the file plus the root's admission profile | per file given the profile |
| `use` resolution and named imports | the importer plus the PUBLIC interface of each imported module, the search path (`REVL_IMPORT_PATH`, stdlib) | per file given imported interfaces |
| merge, privacy, collision and fold checks | every included module's declarations | whole program over interfaces |
| `_check_declared_type_arity`, `_resolve_type_aliases`, `_validate_declared_types`, `_refuse_callable_shadowing` | each declaration plus the global type and callable tables | per declaration given the tables; the tables are whole program |
| `_lower_fns`, `_lower_externs`, `_lower_component`, `_lower_tests` | each body plus every callee signature and type it names | per declaration given the interfaces it reads |
| `_emitting_capabilities`, `_async_callables`, synchronous monomorphs | fixpoints over the whole call graph | whole program |
| taint (`extract_and_normalize`, `check_taint`), model routes and councils | the whole program and the ambient composition | whole program |
| admission profiles, `refuse_admission`, per-root enforcement | the lowered document and every root's profile | whole program |
| `_link` | every component's requires and provides, the running manifest | whole program |
| ownership `annotate_ir` | a summary across all function bodies | whole program |

## Format

One JSON file per unit, `parse-<sha256 of the key>.json`, holding
`{"key": <the key>, "program": <the encoded program>}`. The encoding names
only the parser's own dataclasses (a table built from `revl.parser` at import;
any other name means the entry is not decoded), tuples, frozensets, sets,
dicts with any keys, lists and JSON scalars, and back-references so that an
object reached twice is one object after decoding (the parser's `assets`
registry shares its nodes with the tree). Exact types only: a subclass of a
scalar or a tuple is refused by the encoder and nothing is written.

Loading executes nothing. No pickle, no `eval`, no import of anything the
entry names; the decoder constructs a node with `cls.__new__` and fills its
attributes, and refuses a node that lacks any of its class's fields.

An entry larger than 64 MiB is not read, an entry is opened without
following a symlink where the platform allows it, and every failure to read,
parse or decode an entry (including a nesting deep enough to exhaust the
stack, or memory) is a miss. A failed write removes its temporary file.

## Trust

The cache directory is trusted like the compiler's own files and must be
private to the user who compiles. Keys are computed from public inputs (the
text, the path, the compiler's source), so anyone who can write the directory
can plant an entry that decodes into a parse tree of their choosing, and the
compiler lowers that tree into code. The decoder only limits what an entry
can be (parser nodes and plain data); it cannot tell a planted tree from an
honest one. `compile_files` creates the directory with mode 0o700; it does not
check the mode of a directory that already exists.

## Key

The key is everything the parse is a function of, kept wide:

- the sha256 of the file's text (so an in-memory `sources` substitution is
  keyed by the text it substitutes, like a file on disk);
- the absolute path the file is parsed as, and its path relative to the
  working directory (provenance);
- the compiler: a sha256 over the source of EVERY module of the revl package,
  the revl version, the Python version (the lexer reads `unicodedata`), and
  the entry format. An install without its `.py` sources (bytecode only, or
  zipped) has no source to digest, so on it the cache reads and writes
  nothing.

Admission profiles, `replacing`, the running manifest and `operator_sources`
are not in the key because the parse reads none of them: every step that does
(the no-extern refusal, ref and asset resolution, the merge, admission) runs
after the unit is loaded, on every compile. A later unit that does depend on
any of them carries it in its key.

## Invalidation

An entry is used only when its stored key equals the key computed from the
text this compile read. Otherwise, and whenever the file is unreadable, not
JSON, truncated, of another format, or names anything outside the table, the
unit is rebuilt from the text and the entry rewritten (atomically, through a
temporary file and a rename, so a reader sees a whole entry or none). A parse
that refuses writes nothing and refuses on every compile with its own code and
message. Editing a file changes only that file's key: in this slice a unit
reads only its own text, so its importers keep their units, and the rest of
the compile (which reads interfaces) reruns in full and sees the edit.

Entries of old keys are not removed; a directory grows by one entry per
distinct text. Removing them is left to the caller (see the plan).

## Soundness

Claim: with a cache directory, `compile_files` returns the same document and
raises the same refusals as without one.

1. The parse is deterministic in its inputs. `Parser(text, path).parse()`
   reads no file, no environment variable and no global mutable state; its
   inputs are the text, the path and the compiler's code (including the
   Python standard library it calls). The key holds the text digest, the path,
   a digest of the whole revl package and the Python version.
2. The encoding is lossless for what the parser builds. Every attribute of
   every node is written (not only the dataclass fields: the parser sets
   `_paren` on some expression nodes), shared mutable objects stay shared,
   and container types are exact. A program the encoder cannot express raises
   before anything is written. The identity-keyed tables (`decl_files`,
   `fn_scopes`, `fn_alias_scopes`) are empty after `Parser.parse` (the writer
   refuses otherwise); `decl_files` is filled by the same `record_provenance`
   that `parse_text` runs.
3. A decoded unit is a fresh object graph per compile, so the in-place
   resolution the loader performs next cannot reach an entry or another
   compile.
4. Everything after the parse runs unchanged on the decoded program. The
   checks that could change a verdict (profiles, imports, merging, taint,
   ownership, admission, link) are not kept, so a kept unit cannot turn a
   refusal into an acceptance or the reverse: the same refusal code runs on an
   equal program.

What remains assumed: an iteration over a decoded `set` or `frozenset` visits
the same elements as the original, possibly in another order. A compile whose
output depended on that order would already differ between two processes with
different hash seeds; the corpus check below found no difference. Measured,
the order does differ, at `FnDecl.idents` (a frozenset of identifier
spellings, issue #1900), whose one consumer, `_lower_fns` in `lower.py`, seeds
the fresh-name set of `try` desugaring from it and tests membership only. A
test rebuilds every stored set in reversed order and compares the document.

The identity-keyed tables of `Program` (`decl_files`, `fn_scopes`,
`fn_alias_scopes`) are empty after `Parser.parse`; a unit where any of them is
not is never written, and an entry holding one is not decoded.

Checked: the revl-harness composition gives the same document digest with no
cache, while building the cache and when reading it; every `.rvl` in the revl
tree (examples, stdlib, selfhost and every test fixture: 725 files, 550
documents and 175 refusals) gives the same document or the same refusal
(rendering, code, file, line, message) cold, building and reading.

## What stays whole program, and why

Taint, ownership, admission profiles, the emitting and async fixpoints and
the link stay whole program. Each reads facts from every module (taint flows
and ownership summaries cross function and module boundaries, the fixpoints
are over the global call graph, admission and the link read every component),
so a per-unit result is not the whole-program result without an argument per
analysis that the facts it reads from other units are exactly their exported
interface. No such argument is made here.

## The staged plan

1. Slice 1 (this change): the parse unit, opt-in through
   `compile_files(..., cache_dir=...)`. Saves the parse share (about a fifth)
   less the cost of reading the units.
2. Interfaces: an explicit per-module interface (public functions, types,
   externs and services with their signatures), computed from the parse unit
   and digested. The import wiring and the global tables are then built from
   interfaces, and a later unit's key names the interface digests it read,
   never the importer's text.
3. Per-declaration lowering: keep the lowered form of each function,
   extern and component body (`_lower_fns`, `_lower_component`,
   `_lower_tests`), keyed by the declaration's text and the interface digests
   of every name it resolves. The table-wide checks (`_resolve_type_aliases`,
   arity, shadowing) become per declaration against interface tables, each
   with its own written equivalence argument and a cold-equals-kept corpus
   check.
4. Whole-program analyses over kept units, possibly with per-module summaries
   (an ownership or taint summary of a module's exports), only where an
   argument shows the summary is what the whole-program pass computes.
5. Housekeeping: a size bound or an age-based sweep of the directory, and a
   CLI flag (`revl compile --cache-dir`).
