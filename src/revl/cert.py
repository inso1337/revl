"""Proof-carrying component certificates (roadmap item 474, issue #826).

`revl attest` (roadmap item 127) signs a verdict: this composition was
admitted, and these guarantee codes are the ones the admission checks
cited. That is a signature over the moment of admission, and it says
nothing at all about the formal layer standing behind those codes. Item
474 asks for the other half. A certificate carries the recorded proof
state a component rests on, so a reader sees the coverage AND its
boundaries AND its caveats, rather than one green check.

The certificate is a second envelope, kind `revl.component-certificate`,
MAC'd under its own domain string with the same key machinery the
attestation uses. It carries the identity of the artifact it speaks about
(the canonical IR hash `revl attest` already computes, the source digest,
the file name), the proof model version, one status per catalogued
guarantee, the recorded runtime evidence, and the caveats. Every one of
those readings comes out of an artifact in the tree:

  * `formal/STATUS.md`, the honest per-guarantee map and the sections that
    record what the layer does not cover;
  * `formal/scripts/nonvacuity.tsv`, the non-vacuity registry, one row per
    registered theorem, which is where a `contentless` finding is written
    down;
  * `formal/scripts/run_gate.sh`, the gate's own step list, the theorems it
    name-checks for axioms, and the axiom policy it states;
  * `formal/lean-toolchain` and `formal/lake-manifest.json`, the proof model
    version and its dependency pins.

Two boundaries are worth stating up front, because a certificate that does
not state them is a green check wearing a longer name.

The trust boundary is a key and nothing else. The certificate is MAC'd
under the same key the attestation uses, so it is tamper evident exactly
as far as that key is trustworthy, and it proves authorship rather than
honesty: a key holder authors freely. The key is read from a file or an
environment variable and is never passed in argv.

The proof boundary is the artifacts. A certificate can only claim what the
documents in the tree record, and every reading is the same clause grammar
the status cells are written in, so a guarantee whose theorems are not
registered cannot be certified as proved, and a certificate that does not
carry the gaps of its partial rows does not build at all. A document that
says nothing about a guarantee yields a refusal rather than a weaker
claim, because silence is not evidence.

The verification boundary is member by member, because a MAC proves authorship
and a key holder authors freely. `revl attest --verify-certificate` re-derives
every member whose value the artifacts, the key or the verifier's own build
fixes, and compares it with what was signed rather than reading it: the
artifact digests, the per-guarantee rows including their status cells, their
gaps, their registered theorems and their contentless findings, the caveats,
the requirements and the check recorded behind each one, the proof model pins,
the checker identity, the key fingerprint, the subject's source digest and the
commit the certificate names. Two members are recorded rather than re-derived,
because they are statements about the signing EVENT rather than about the tree:
`timestamp` and `signer`. They are inside the MAC, so they cannot be edited
after the fact, and every successful verification says in so many words that
they were not re-derived, so a green check is never read as a claim about them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
from pathlib import Path

from . import attest
from .attest import NotCanonicalizable
from .errors import RevlError

CERT_KIND = "revl.component-certificate"
CERT_VERSION = "1.0"
CERT_SIGN_DOMAIN = b"revl.component-certificate/v1\x00"

#: The signed members no verifier can re-derive, because they are statements
#: about the signing EVENT rather than about the tree: the instant the signer
#: signed and the label it chose for itself. They are inside the MAC, so they
#: cannot be edited after signing, and `verify_certificate` names them as
#: recorded rather than re-derived in every result it returns, so a green check
#: is never read as a claim about them.
UNVERIFIABLE = ("timestamp", "signer")

#: Where the formal package lives, when it is not found by walking up from
#: this file or from the working directory.
FORMAL_DIR_ENV = "REVL_FORMAL_DIR"

#: The per-guarantee status vocabulary. Multi-valued on purpose: item 474's
#: own framing ("full", "partial", "unproved") cannot hold G5, whose lattice
#: statement is proved while its shape-level statement is contentless, so a
#: status cell that says both is carried as `partial` plus the cell verbatim
#: plus the registry's `contentless` finding, never flattened to one word.
PROVED = "proved"
PARTIAL = "partial"
UNPROVED = "unproved"
STATUSES = (PROVED, PARTIAL, UNPROVED)

REQ_GUARANTEE = "revl.guarantee-status"
REQ_CONTENTLESS = "revl.contentless-statement"
REQ_UNSTATABLE = "revl.unstatable-obligation"
REQ_REGISTRY = "revl.non-vacuity-registry"
REQ_MAP = "revl.guarantee-map"
REQ_GATE = "revl.formal-gate"
REQ_MODEL = "revl.proof-model"
REQ_ORACLE = "revl.oracle-census"
REQ_INJECTION = "revl.injection-proof"
REQ_SWEEP = "revl.injection-sweep"
REQUIREMENT_KINDS = (
    REQ_GUARANTEE, REQ_CONTENTLESS, REQ_UNSTATABLE, REQ_REGISTRY, REQ_MAP,
    REQ_GATE, REQ_MODEL, REQ_ORACLE, REQ_INJECTION, REQ_SWEEP,
)

#: The artifacts every reading comes out of, and the digest of each in the
#: envelope. A certificate is bound to these exact revisions: a tree whose
#: formal package differs fails verification and says which artifact moved,
#: which is the difference between checking a certificate and reading it.
ARTIFACT_NAMES = ("gate", "registry", "status")

MAP_HEADING = "## What the layer covers, and what it does not"
UNSTATABLE_HEADING = "What G9 does NOT cover"


class CertError(RevlError):
    """A certificate could not be built, or could not be re-derived.

    Raised at the certificate's own level rather than at a source location:
    what is wrong is the record or the evidence behind it, not a line of the
    composition, so the diagnostic names the certificate as its origin."""

    def __init__(self, message: str, filename: str = "<certificate>"):
        super().__init__(filename, 0, message)


# --------------------------------------------------------------------------- #
# the artifacts                                                               #
# --------------------------------------------------------------------------- #
def formal_root(explicit: str | Path | None = None, env=None) -> Path:
    """Locate the formal package: the directory holding `STATUS.md`.

    In order, the `explicit` path, `$REVL_FORMAL_DIR`, `formal/` under the
    working directory, then `formal/` under any ancestor of this file (which
    is where it sits in a checkout). A missing package is an error rather
    than an empty status map: a certificate whose evidence is absent is
    exactly the artifact this verb exists to prevent.

    A named directory is not a preference. When `explicit` is given and holds
    no `STATUS.md`, this refuses rather than falling through to a package found
    elsewhere: an operator who points the verb at one formal package must not
    be handed a certificate built from another."""
    if env is None:
        import os  # noqa: PLC0415  (lazy, so importing this module has no side effect)
        env = os.environ
    if explicit is not None:
        named = Path(explicit)
        if (named / "STATUS.md").is_file():
            return named.resolve()
        raise CertError(
            f"--formal {explicit} holds no STATUS.md, so it is not a formal "
            "package: a certificate reads its per-guarantee evidence out of "
            "one, and substituting a different package for the one that was "
            "named would be silently certifying something else")
    candidates: list[Path] = []
    configured = env.get(FORMAL_DIR_ENV)
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path.cwd() / "formal")
    here = Path(__file__).resolve()
    candidates.extend(parent / "formal" for parent in here.parents)
    for candidate in candidates:
        if (candidate / "STATUS.md").is_file():
            return candidate.resolve()
    tried = ", ".join(str(candidate) for candidate in candidates)
    raise CertError(
        "no formal package: a component certificate reads its per-guarantee "
        f"evidence out of formal/STATUS.md, so there is nothing to certify "
        f"without one. Pass --formal DIR or set {FORMAL_DIR_ENV} (tried {tried})")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CertError(f"cannot read {path}: {error}") from error


def _relative(root: Path, path: Path) -> str:
    """The path as a reader of the certificate sees it, relative to the tree
    the formal package sits in (`formal/STATUS.md`, not an absolute path that
    only means something on the signing machine)."""
    try:
        return str(path.relative_to(root.parent))
    except ValueError:
        return str(path)


def sha256_text(text: str) -> str:
    """The sha256 of a text artifact, hex. What binds a certificate to the
    exact revision of a document it read."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """The sha256 of a source file's bytes, hex."""
    return hashlib.sha256(data).hexdigest()


# --- reading the map ------------------------------------------------------- #
_MAP_ROW = re.compile(
    r"^\|\s*\*\*(G\d+)\*\*\s*([^|]*?)\s*"   # code, prose name
    r"\|\s*([^|]+?)\s*"                      # the status cell
    r"\|\s*([^|]*?)\s*"                      # the theorem-count cell
    r"\|\s*([^|]*?)\s*"                      # the oracle cell
    r"\|\s*([^|]*?)\s*\|\s*$"                # the gap cell
)
_WORD_PROVED = re.compile(r"\bproved\b")


def _section(text: str, heading: str, level: int = 2) -> str | None:
    """The section starting at `heading`, up to the next heading of the same
    level. `None` when the document has no such heading."""
    start = text.find(heading)
    if start < 0:
        return None
    marker = "\n" + "#" * level + " "
    stop = text.find(marker, start + len(heading))
    return text[start:] if stop < 0 else text[start:stop]


def _flatten(text: str) -> str:
    """Markdown prose as one line, with emphasis dropped. Applied to prose the
    certificate quotes into a caveat, so a reader gets the sentence rather than
    the source's line breaks."""
    return _one_line(text.replace("**", "").replace("`", ""))


def _one_line(text: str) -> str:
    """Whitespace collapsed. The reading that keeps a verbatim quote verbatim:
    a table cell is one line already, and a paragraph's own wrapping is not
    part of what it says."""
    return re.sub(r"\s+", " ", text).strip()


def guarantee_map(text: str, *, source: str) -> dict[str, dict]:
    """The per-guarantee map: code -> the row's cells, verbatim.

    The map is the table under `## What the layer covers, and what it does
    not`, and its rows are the ones whose first cell is a bolded guarantee
    code. Rows elsewhere in the document (the sections that restate a single
    guarantee's row, or the `A`/`T`/`R` rows that are not guarantee codes)
    are not this table."""
    section = _section(text, MAP_HEADING)
    if section is None:
        raise CertError(
            f"{source} has no `{MAP_HEADING}` section, so it records no "
            "per-guarantee status and a certificate would have nothing to "
            "carry for any guarantee")
    rows: dict[str, dict] = {}
    for line in section.splitlines():
        match = _MAP_ROW.match(line)
        if match is None:
            continue
        code, name, status_cell, theorems, oracle, gap = (
            group.strip() for group in match.groups())
        rows[code] = {
            "code": code, "name": name, "status_cell": status_cell,
            "theorems_cell": theorems, "oracle_cell": oracle, "gap": gap,
        }
    if not rows:
        raise CertError(
            f"{source}'s `{MAP_HEADING}` section carries no guarantee row; a "
            "map with no rows is not evidence of anything")
    return rows


def status_of(cell: str, *, source: str, code: str) -> str:
    """The status a map row's status cell states, as one of `STATUSES`.

    The cell is prose, and the honest reading of prose is the weakest thing
    it will admit rather than the strongest. A cell that mixes a proved rule
    with an unproved obligation (`rule proved; coverage unproved and
    unstatable`) is `partial`, because the row does not buy the guarantee's
    obligation outright; a cell that says `full` and then qualifies which
    statement is weak is `proved`, with the qualification carried verbatim
    beside it. A cell this reading cannot place is a refusal, not a guess:
    an unreadable status is the one thing a proof-carrying certificate must
    not paper over."""
    low = cell.lower()
    head = low.split(";", 1)[0].strip().strip("*").strip()
    if head.startswith("full"):
        return PROVED
    if "partial" in low or head.startswith("partial"):
        return PARTIAL
    if "unproved" in low:
        if _WORD_PROVED.search(low) or "unstatable" in low:
            return PARTIAL
        return UNPROVED
    if head.startswith("none"):
        return UNPROVED
    raise CertError(
        f"{source}'s {code} row states a status this reading cannot place "
        f"({cell!r}); a certificate reports what the document says, so an "
        "unreadable row is refused rather than rounded")


def unstatable_gap(text: str) -> str | None:
    """The sentence that records a guarantee whose obligation cannot be
    stated at all, which is a stronger admission than an unproved one."""
    section = _section(text, UNSTATABLE_HEADING, 3)
    if section is None:
        return None
    for sentence in _flatten(section).split(". "):
        if "unstatable" in sentence and "UNPROVED" in sentence:
            return sentence.strip().rstrip(".") + "."
    return None


# --- reading the registry -------------------------------------------------- #
def registry(text: str, *, source: str) -> dict[str, dict]:
    """The non-vacuity registry: theorem -> {kind, witnesses, note}.

    One row per theorem registered in the axioms gate, naming the concrete
    evidence that its hypotheses can all hold at once. The registry is the
    machine-readable half of the honest status: it is where a statement that
    is true by definition is written down as `contentless` rather than
    counted as a proof."""
    rows: dict[str, dict] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 4:
            raise CertError(
                f"{source} has a row with {len(fields)} fields, expected 4 "
                f"(theorem, kind, witnesses, note): {line!r}")
        theorem, kind, witnesses, note = (field.strip() for field in fields[:4])
        rows[theorem] = {
            "kind": kind,
            "witnesses": [w.strip() for w in witnesses.split(",") if w.strip()],
            "note": note,
        }
    if not rows:
        raise CertError(f"{source} registers no theorem, so nothing backs any status")
    return rows


def _registered(rows: dict[str, dict], code: str) -> list[str]:
    """The registered theorems a guarantee's rows cite, by name prefix. `G4`'s
    lattice form lives in `RevL.G4Classified`, so the prefix has to allow the
    classified suffix rather than assuming one file per guarantee."""
    pattern = re.compile(rf"^RevL\.{code}(Classified)?\.")
    return sorted(name for name in rows if pattern.match(name))


# --- reading the gate ------------------------------------------------------ #
_NUMBERED_STEP = re.compile(r"^#\s*(\d)\.\s+(.*\S)\s*$", re.MULTILINE)
_GATE_THEOREM = re.compile(r"^  (RevLOracle\.[A-Za-z0-9_.']+|RevL\.[A-Za-z0-9_.']+)", re.MULTILINE)
_AXIOM_POLICY = re.compile(r"\(no sorryAx[^)]*\)")


def gate_steps(text: str) -> list[str]:
    """The gate's numbered step list, as written in the script's header."""
    return [f"{number}. {body}" for number, body in _NUMBERED_STEP.findall(text)]


def gate_theorems(text: str) -> tuple[list[str], list[str]]:
    """The theorems the gate name-checks for axioms: the `RevL` library names
    and the oracle bridge names, in the two blocks the script runs."""
    names = _GATE_THEOREM.findall(text)
    bridge = [name for name in names if name.startswith("RevLOracle.")]
    library = [name for name in names if not name.startswith("RevLOracle.")]
    return library, bridge


# --- reading the oracle's evidence ----------------------------------------- #
_CENSUS_SHAPE = re.compile(
    r"(\d+)\s*\.rvl files?[^0-9]*?(\d+)\s*components?[^0-9]*?(\d+)\s*statements?")
_CENSUS_VERDICTS = re.compile(
    r"(\d+)\s*verdicts? compared.*?(\d+)\s*agree[,\s]+(\d+)\s*mismatches?",
    re.DOTALL)
_INJECTION_HEADER = ("injection", "caught by", "result")
_SWEEP_SENTENCE = re.compile(
    r"Perturbing\s+the shipped side one invariant at a time")
_SWEEP_ITEM = re.compile(r"([^,;]+?\(\s*(\d+)\s*(?:mismatches?)?\))")


def oracle_census(text: str, *, source: str) -> dict:
    """The differential oracle's census: what the row compares, and how many
    of those comparisons agree. Numbers a reader can check against the
    harness's own output rather than a claim that something was checked."""
    shape = _CENSUS_SHAPE.search(text)
    verdicts = _CENSUS_VERDICTS.search(text)
    if shape is None or verdicts is None:
        raise CertError(
            f"{source} carries no readable oracle census, so the certificate "
            "cannot report what the proved model was compared against")
    files, components, statements = (int(value) for value in shape.groups())
    compared, agree, mismatches = (int(value) for value in verdicts.groups())
    return {
        "files": files, "components": components, "statements": statements,
        "verdicts_compared": compared, "verdicts_agree": agree,
        "mismatches": mismatches,
    }


def injection_proofs(text: str, *, source: str) -> list[dict]:
    """The injections the document records as caught, each with the surface
    that caught it. A row is worth more than a green check: it is a record of
    the artifact having been seen to fail before it was trusted."""
    rows: list[dict] = []
    header = False
    for line in text.splitlines():
        if not line.startswith("|"):
            header = False
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if tuple(cell.lower() for cell in cells) == _INJECTION_HEADER:
            header = True
            continue
        if not header or set("".join(cells)) <= set("-: "):
            continue
        if len(cells) != 3:
            raise CertError(f"{source}'s injection table has a {len(cells)}-cell row: {line!r}")
        rows.append({"injection": cells[0], "caught_by": cells[1], "result": cells[2]})
    if not rows:
        raise CertError(
            f"{source} records no injection proof, so the certificate cannot "
            "report that the evidence was ever seen to fail")
    return rows


def injection_sweep(text: str, *, source: str = "formal/STATUS.md") -> list[dict]:
    """The per-invariant perturbations the document lists beside the injection
    table: each one names an invariant and the number of mismatches it
    reddens the row with. Read from the sentence's own comma-separated list
    rather than from a pattern for the first entry, so a sweep that grew new
    invariants is reported in full or not at all."""
    paragraph = None
    for block in text.split("\n\n"):
        if _SWEEP_SENTENCE.search(block):
            paragraph = _one_line(block)
            break
    if paragraph is None:
        raise CertError(
            f"{source} no longer lists the per-invariant injection sweep, so "
            "the certificate cannot report what the row was watched to fail on")
    tail = _SWEEP_SENTENCE.split(paragraph, 1)[1]
    rows: list[dict] = []
    for phrase, count in _SWEEP_ITEM.findall(tail):
        name = re.sub(r"^[^`\w]+", "", phrase).strip()
        name = re.sub(r"[\s-]+$", "", name)
        if not name:
            raise CertError(f"{source}'s sweep names an empty invariant: {phrase!r}")
        rows.append({"invariant": name, "effect": f"{int(count)} mismatches"})
    if not rows:
        raise CertError(
            f"{source}'s per-invariant sweep lists no mismatch count, so the "
            "certificate cannot report that any invariant was watched to fail")
    return rows


def proof_model(root: Path, *, source: str | None = None) -> dict:
    """The proof model: the Lean release the theorems were checked against,
    plus the pins of the package's own dependencies."""
    toolchain_path = root / "lean-toolchain"
    if not toolchain_path.is_file():
        raise CertError(
            f"no {_relative(root, toolchain_path)}: a certificate names the "
            "proof model its theorems were checked against, and a package "
            "without a pinned toolchain has not named one")
    toolchain = _read(toolchain_path).strip()
    if not toolchain:
        raise CertError(f"{_relative(root, toolchain_path)} is empty")
    manifest_path = root / "lake-manifest.json"
    manifest = _read(manifest_path) if manifest_path.is_file() else None
    return {
        "lean_toolchain": toolchain,
        "manifest_digest": sha256_text(manifest) if manifest is not None else None,
        "dependencies": [],
    }


def _dependencies(manifest_text: str | None) -> list[str]:
    """The package names in `lake-manifest.json`, which is where a pinned
    dependency would be visible. Revl's formal layer has none by design, and
    a certificate that says so is saying something checkable."""
    if not manifest_text:
        return []
    import json  # noqa: PLC0415  (one local decode, no module-level side effect)
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        raise CertError(f"formal/lake-manifest.json is not JSON: {error}") from error
    packages = manifest.get("packages") or []
    return sorted(str(package.get("name")) for package in packages)


def tree_commit(root: Path) -> str | None:
    """The commit the artifacts were read at, when the tree is a checkout that
    can answer. Absent rather than fatal: a certificate built from an exported
    tarball still names every artifact by digest, and the commit is a
    convenience on top of that."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def commit_in_history(root: Path, commit: str) -> tuple[bool | None, str]:
    """Is the commit a certificate names one this tree's history contains?

    `(None, reason)` when the package on this machine is not a checkout and the
    question cannot be asked here at all. The commit is the checkout the signer
    had, so a package that has since moved forward is not a moved certificate:
    what is checkable is that the named commit is a revision of THIS history,
    which is what makes a fabricated commit id a refusal rather than a
    footnote."""
    head = tree_commit(root)
    if head is None:
        return None, "the formal package on this machine is not a git work tree"
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", commit, "HEAD"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"git could not answer ({error})"
    if done.returncode == 0:
        return True, ""
    return False, (f"no commit {commit} in this repository's history "
                   f"(HEAD is {head[:12]})")


# --------------------------------------------------------------------------- #
# the state, and its requirements                                             #
# --------------------------------------------------------------------------- #
def _requirement(kind: str, subject: str, source: str, detail: str, check: str) -> dict:
    return {"kind": kind, "subject": subject, "source": source,
            "detail": detail, "check": check}


def formal_state(root: Path, *, guarantees: list[str] | None = None) -> dict:
    """Read the recorded proof state out of the formal package.

    Every status is derived here and nowhere else: from the map row that
    states it, the registry row that backs it, and the gate that lists the
    theorem for an axiom check. `certifiable` then refuses the whole document
    when a guarantee has no row, no registered theorem, or a gap that was
    dropped, which is the assertion that makes a certificate say less rather
    than more."""
    if guarantees is None:
        guarantees = attest.catalogued_guarantees()
    status_path = root / "STATUS.md"
    registry_path = root / "scripts" / "nonvacuity.tsv"
    gate_path = root / "scripts" / "run_gate.sh"
    for path in (status_path, registry_path, gate_path):
        if not path.is_file():
            raise CertError(
                f"no {_relative(root, path)}: the per-guarantee status is read "
                "from the recorded artifacts, and a certificate without them "
                "would be asserting coverage it cannot show")

    status_text = _read(status_path)
    status_source = _relative(root, status_path)
    registry_text = _read(registry_path)
    registry_source = _relative(root, registry_path)
    gate_text = _read(gate_path)
    gate_source = _relative(root, gate_path)

    rows = guarantee_map(status_text, source=status_source)
    registered = registry(registry_text, source=registry_source)
    library, bridge = gate_theorems(gate_text)
    steps = gate_steps(gate_text)
    if not steps or not library or not bridge:
        raise CertError(
            f"{gate_source} no longer reads as the formal gate (steps, library "
            f"theorems, bridge theorems: {len(steps)}, {len(library)}, "
            f"{len(bridge)}), so a certificate cannot report what it checks")
    policy = _AXIOM_POLICY.search(gate_text)
    if policy is None:
        raise CertError(f"{gate_source} states no axiom policy")

    statuses: list[dict] = []
    for code in sorted(guarantees):
        row = rows.get(code)
        if row is None:
            raise CertError(
                f"{status_source} carries no row for {code}, so the recorded "
                "proof state does not cover this guarantee")
        status = status_of(row["status_cell"], source=status_source, code=code)
        cited = _registered(registered, code)
        statuses.append({
            "code": code,
            "name": row["name"],
            "status": status,
            "status_cell": row["status_cell"],
            "theorems_cell": row["theorems_cell"],
            "oracle_cell": row["oracle_cell"],
            "gap": row["gap"],
            "registered": cited,
            "contentless": sorted(name for name in cited
                                  if registered[name]["kind"] == "contentless"),
        })

    unstatable = unstatable_gap(status_text)
    census = oracle_census(status_text, source=status_source)
    injections = injection_proofs(status_text, source=status_source)
    sweep = injection_sweep(status_text)
    model = proof_model(root)
    model["dependencies"] = _dependencies(
        _read(root / "lake-manifest.json") if (root / "lake-manifest.json").is_file() else None)

    kinds: dict[str, int] = {}
    for row in registered.values():
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1

    requirements: list[dict] = []
    for status in statuses:
        detail = f"{status['status']}: {status['status_cell']}"
        if status["theorems_cell"]:
            detail += f" (theorems: {status['theorems_cell']})"
        requirements.append(_requirement(
            REQ_GUARANTEE, status["code"], status_source, detail,
            "the map row's status cell, plus the registry rows under the "
            "guarantee's name prefix"))
        if status["contentless"]:
            requirements.append(_requirement(
                REQ_CONTENTLESS, status["code"], registry_source,
                "contentless by definition: "
                + "; ".join(f"{name} ({registered[name]['note']})"
                            for name in status["contentless"]),
                f"the registry row's `kind` column in {registry_source}"))
    if unstatable is not None:
        requirements.append(_requirement(
            REQ_UNSTATABLE, "G9", status_source, f"coverage obligation: {unstatable}",
            f"the sentence holding `UNPROVED, unstatable` under "
            f"`{UNSTATABLE_HEADING}` in {status_source}"))
    requirements.append(_requirement(
        REQ_REGISTRY, registry_source, registry_source,
        f"{len(registered)} registered theorems ("
        + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
        + "), all named in " + status_source,
        "formal/scripts/nonvacuity_gate.py"))
    requirements.append(_requirement(
        REQ_MAP, status_source, status_source,
        f"{len(rows)} guarantee rows, covering registers "
        + ", ".join(sorted(rows)),
        "one map row per catalogued guarantee code"))
    requirements.append(_requirement(
        REQ_GATE, gate_source, gate_source,
        f"{len(steps)} steps; {len(library)} library theorems and "
        f"{len(bridge)} oracle bridge theorems name-checked; policy "
        f"{policy.group(0)}",
        "formal/scripts/axioms_gate.py over the gate's own lists"))
    requirements.append(_requirement(
        REQ_MODEL, model["lean_toolchain"], _relative(root, root / "lean-toolchain"),
        f"lake manifest {str(model['manifest_digest'])[:12]}"
        + (f", dependencies {', '.join(model['dependencies'])}"
           if model["dependencies"] else ", no dependencies"),
        "formal/lean-toolchain and formal/lake-manifest.json"))
    requirements.append(_requirement(
        REQ_ORACLE, "formal/harness/diff_corpus.py", status_source,
        f"{census['files']} files, {census['components']} components, "
        f"{census['statements']} statements; {census['verdicts_compared']} "
        f"verdicts compared, {census['verdicts_agree']} agree, "
        f"{census['mismatches']} mismatches",
        "the census paragraph in " + status_source))
    for row in injections:
        requirements.append(_requirement(
            REQ_INJECTION, row["injection"], status_source,
            f"caught by {row['caught_by']}: {row['result']}",
            "the injection table's rows in " + status_source))
    for row in sweep:
        requirements.append(_requirement(
            REQ_SWEEP, row["invariant"], status_source, row["effect"],
            "the per-invariant sweep beside the injection table in " + status_source))

    caveats: list[str] = []
    for status in statuses:
        if status["status"] != PROVED:
            caveats.append(f"{status['code']}: {status['status']}, {status['gap']}")
        elif ";" in status["status_cell"]:
            caveats.append(f"{status['code']}: {status['status_cell']}")
        if status["contentless"]:
            caveats.append(
                f"{status['code']}: {', '.join(status['contentless'])} is "
                "contentless, a statement true by definition rather than by "
                "the property it is named for")
    if unstatable is not None:
        caveats.append(f"G9: {unstatable}")

    return {
        "statuses": statuses,
        "caveats": caveats,
        "requirements": requirements,
        "proof_model": model,
        "artifacts": {
            "status": {"path": status_source, "digest": sha256_text(status_text)},
            "registry": {"path": registry_source, "digest": sha256_text(registry_text)},
            "gate": {"path": gate_source, "digest": sha256_text(gate_text)},
        },
        "census": census,
        "as_of_commit": tree_commit(root),
    }

def certifiable(state: dict, guarantees: list[str] | None = None) -> None:
    """Refuse a status set that cannot be fully backed by the artifacts.

    Four refusals, each of them a way for a certificate to claim more than
    the tree shows:

      * a catalogued guarantee with no map row, or a map row for a code the
        guarantee catalogue does not define;
      * a guarantee with no registered theorem under its name prefix, because
        a status with nothing registered behind it is a status that no gate
        checks;
      * a guarantee that is not `proved` and carries no recorded gap, because
        the gap is the part of the status the reader needs;
      * a status outside `STATUSES`, or an empty requirement list."""
    if guarantees is None:
        guarantees = attest.catalogued_guarantees()
    requirements = state["requirements"]
    if not requirements:
        raise CertError("a certificate with no requirements carries no evidence")
    by_code = {row["code"]: row for row in state["statuses"]}
    missing = [code for code in guarantees if code not in by_code]
    if missing:
        raise CertError(
            f"the recorded proof state has no status for {', '.join(missing)}, "
            "so a certificate cannot report this component's coverage")
    unknown = sorted(set(by_code) - set(guarantees))
    if unknown:
        raise CertError(
            f"the recorded proof state claims {', '.join(unknown)}, which the "
            "composition guarantee catalogue does not define")
    unbacked = [code for code in guarantees if not by_code[code]["registered"]]
    if unbacked:
        raise CertError(
            f"{', '.join(unbacked)} has no theorem in the non-vacuity "
            "registry, so its status is asserted rather than checked")
    uncaveated = [code for code, row in sorted(by_code.items())
                  if row["status"] != PROVED and not row["gap"]]
    if uncaveated:
        raise CertError(
            f"{', '.join(uncaveated)} is not proved and records no gap, so "
            "the certificate would report a weaker guarantee with no reason "
            "attached")
    for row in state["statuses"]:
        if row["status"] not in STATUSES:
            raise CertError(f"{row['code']} has status {row['status']!r}, which is not one of {STATUSES}")
    for requirement in requirements:
        if requirement["kind"] not in REQUIREMENT_KINDS:
            raise CertError(f"unknown requirement kind {requirement['kind']!r}")


# --------------------------------------------------------------------------- #
# the certificate                                                             #
# --------------------------------------------------------------------------- #
def source_digest(path: str | Path) -> str:
    """The sha256 of the source file the certificate speaks about, which is
    the artifact a reader can identify without the IR."""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise CertError(f"cannot read {path}: {error}") from error
    return sha256_bytes(data)


def load_certificate(path: str) -> dict:
    """Load a certificate JSON document from disk. A record that is not a JSON
    object is refused here rather than at verification, so the caller never has
    to decide what a list means."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CertError(f"cannot read certificate: {error}", str(path)) from error
    if not isinstance(document, dict):
        raise CertError("certificate is not a JSON object", str(path))
    return document


def _sign(body: dict, key: bytes) -> str:
    """The HMAC-SHA256 over the certificate's canonical bytes, hex. The MAC is
    taken over a domain tag of its own: a certificate and an attestation are
    two protocols signed with one key, so a certificate must not verify as an
    attestation or the reverse."""
    signed = {member: value for member, value in body.items()
              if member != attest.SIGNATURE_FIELD}
    return hmac.new(key, CERT_SIGN_DOMAIN + attest._canonical_bytes(signed),
                    hashlib.sha256).hexdigest()


def make_certificate(ir: dict, key: bytes, *, verdict=None,
                     source_path: str | Path | None = None,
                     formal: str | Path | None = None,
                     now=None, signer: str | None = None,
                     evidence_bindings: dict | None = None) -> dict:
    """Build a signed component certificate for an admitted composition.

    The verdict is required and must come from `attest.run_gate`, for the same
    reason an attestation requires one: a certificate records what was
    measured, and there is nothing to record until the frontend has run. The
    per-guarantee state is then read from the formal artifacts, and
    `certifiable` refuses the document outright when the artifacts do not back
    it.
    """
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise CertError("signing key must be non-empty bytes")
    if not isinstance(verdict, attest.GateVerdict):
        raise CertError(
            "certifying needs a gate verdict from `attest.run_gate`: the "
            "certificate states what was measured about this composition, so "
            "there is nothing to record without a run")
    if not verdict.admitted:
        raise CertError(
            f"the gate did not admit this composition, so there is no admitted "
            f"verdict to certify: {verdict.reason}")
    ir_hash = attest.canonical_hash(ir)
    if not hmac.compare_digest(ir_hash, str(verdict.composition_hash)):
        raise CertError(
            "the gate verdict is for a different composition: the gate admitted "
            f"{str(verdict.composition_hash)[:12]}, the IR being certified hashes "
            f"to {ir_hash[:12]}")

    guarantees = sorted(set(verdict.guarantees))
    unknown = [code for code in guarantees if code not in attest.catalogued_guarantees()]
    if unknown or not guarantees:
        raise CertError(
            "the gate verdict records no usable guarantee codes "
            f"({', '.join(unknown) or '(empty)'} is not in the composition "
            "guarantee catalogue)")

    root = formal_root(formal)
    state = formal_state(root, guarantees=attest.catalogued_guarantees())
    certifiable(state, attest.catalogued_guarantees())

    if source_path is None or not Path(source_path).name:
        raise CertError(
            "certifying needs the source file the gate ran over: the subject "
            "hash binds the certificate to one revision of one artifact, so a "
            "caller that read the composition from somewhere else must name "
            "the file it read")
    subject = {
        "filename": str(source_path),
        "source_hash": source_digest(source_path),
        "composition_hash": ir_hash,
        "guarantees": guarantees,
    }
    body = {
        "kind": CERT_KIND,
        "version": CERT_VERSION,
        "verdict": attest.VERDICT_ADMITTED,
        "hash_alg": attest.HASH_ALG,
        "subject": subject,
        "proof_model": dict(sorted(state["proof_model"].items())),
        "as_of_commit": state["as_of_commit"],
        "artifacts": state["artifacts"],
        "statuses": state["statuses"],
        "caveats": state["caveats"],
        "requirements": state["requirements"],
        "checker": dict(sorted(attest.checker_identity().items())),
        "timestamp": attest._now_iso(now),
        "sign_alg": attest.SIGN_ALG,
        "signer": signer,
        "key_id": attest.key_id(bytes(key)),
    }
    if evidence_bindings:
        body["evidence_bindings"] = dict(sorted(evidence_bindings.items()))
    return {**body, attest.SIGNATURE_FIELD: _sign(body, bytes(key))}


# --- verification ---------------------------------------------------------- #
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_HEX16 = re.compile(r"\A[0-9a-f]{16}\Z")
#: A commit id as `git rev-parse HEAD` writes it: sha-1 and sha-256 objects
#: alike, abbreviated or not, so the field is checked for being a commit id
#: rather than for being forty characters.
_COMMIT = re.compile(r"\A[0-9a-f]{7,64}\Z")


def _validate_envelope(cert: dict) -> str:
    """Is this record a component certificate of the shape this verifier
    accepts? Returns a refusal reason, or `""`.

    A MAC proves authorship and nothing else, and a key holder authors
    freely, so every member whose value carries a fixed meaning is checked
    against that meaning:

      * `kind`/`version`, so a `revl.attestation` or a v2 certificate with
        different members is not read as a v1 certificate;
      * `verdict`, which for a certificate is `admitted` and nothing else;
      * the subject hashes, which must be sha256 digests, so a certificate
        cannot name its subject with a truncated or empty hash;
      * `guarantees`, whose codes must come from the catalogue, so a
        certificate cannot carry a guarantee the compiler does not define;
      * `statuses`, one per catalogued code, with a status from the
        vocabulary and a non-empty cell, so a certificate cannot report a
        status it does not name or name one twice;
      * `requirements`, whose members must be present and whose kinds must
        be ones this module derives, so a certificate cannot be padded with
        an evidence kind no verifier knows how to re-derive.
    """
    def _reason(member, expected, found):
        return f"envelope refused: {member} is {found!r}, expected {expected!r}"

    for member, expected in (("kind", CERT_KIND),
                             ("version", CERT_VERSION),
                             ("verdict", attest.VERDICT_ADMITTED),
                             ("sign_alg", attest.SIGN_ALG),
                             ("hash_alg", attest.HASH_ALG)):
        found = cert.get(member)
        if found != expected:
            return _reason(member, expected, found)

    if not _HEX16.match(str(cert.get("key_id"))):
        return f"envelope refused: key_id is not a key fingerprint ({cert.get('key_id')!r})"
    timestamp = cert.get("timestamp")
    if not isinstance(timestamp, str) or attest._parse_iso(timestamp) is None:
        return f"envelope refused: timestamp is not an ISO-8601 instant ({timestamp!r})"
    commit = cert.get("as_of_commit")
    if commit is not None and not _COMMIT.match(str(commit)):
        return (f"envelope refused: as_of_commit is not a commit id "
                f"({commit!r})")

    subject = cert.get("subject")
    if not isinstance(subject, dict):
        return "envelope refused: no `subject` member, so nothing says what this certificate is about"
    if not isinstance(subject.get("filename"), str) or not subject["filename"]:
        return f"envelope refused: subject.filename is not a path ({subject.get('filename')!r})"
    for member in ("source_hash", "composition_hash"):
        if not _HEX64.match(str(subject.get(member))):
            return (f"envelope refused: subject.{member} is not a sha256 digest "
                    f"({subject.get(member)!r})")
    guarantees = subject.get("guarantees")
    known = attest.catalogued_guarantees()
    if (not isinstance(guarantees, list) or not guarantees
            or not all(isinstance(code, str) for code in guarantees)):
        return f"envelope refused: subject.guarantees is not a non-empty list of codes ({guarantees!r})"
    unknown = [code for code in guarantees if code not in known]
    if unknown:
        return ("envelope refused: subject.guarantees names "
                f"{', '.join(repr(code) for code in unknown)}, which the "
                f"composition guarantee catalogue does not define (known: {', '.join(known)})")
    if list(guarantees) != sorted(set(guarantees)):
        return f"envelope refused: subject.guarantees must be sorted and free of duplicates ({guarantees!r})"

    model = cert.get("proof_model")
    if not isinstance(model, dict) or not isinstance(model.get("lean_toolchain"), str) \
            or not model["lean_toolchain"]:
        return f"envelope refused: proof_model names no Lean toolchain ({model!r})"
    if set(model) != {"lean_toolchain", "manifest_digest", "dependencies"}:
        return (f"envelope refused: proof_model is not the toolchain, the manifest "
                f"digest and the dependency pins ({sorted(str(member) for member in model)})")
    if model["manifest_digest"] is not None \
            and not _HEX64.match(str(model["manifest_digest"])):
        return (f"envelope refused: proof_model.manifest_digest is not a sha256 digest "
                f"({model['manifest_digest']!r})")
    if not isinstance(model["dependencies"], list) \
            or not all(isinstance(name, str) and name for name in model["dependencies"]):
        return (f"envelope refused: proof_model carries no list of dependency pins "
                f"({model['dependencies']!r})")

    statuses = cert.get("statuses")
    if not isinstance(statuses, list) or not statuses:
        return f"envelope refused: statuses is not a non-empty list ({statuses!r})"
    seen: list[str] = []
    for row in statuses:
        if not isinstance(row, dict):
            return f"envelope refused: a status row is not an object ({row!r})"
        code = row.get("code")
        if code not in known:
            return (f"envelope refused: a status row names {code!r}, which the "
                    f"guarantee catalogue does not define (known: {', '.join(known)})")
        if row.get("status") not in STATUSES:
            return (f"envelope refused: {code} has status {row.get('status')!r}, "
                    f"which is not one of {STATUSES}")
        if not isinstance(row.get("status_cell"), str) or not row["status_cell"]:
            return f"envelope refused: {code} carries no status cell to explain its status"
        if not isinstance(row.get("gap"), str):
            return f"envelope refused: {code} carries no gap field"
        if not isinstance(row.get("name"), str) or not row["name"]:
            return f"envelope refused: {code} names no guarantee ({row.get('name')!r})"
        for member in ("theorems_cell", "oracle_cell"):
            if not isinstance(row.get(member), str):
                return f"envelope refused: {code} carries no {member} ({row.get(member)!r})"
        if not isinstance(row.get("registered"), list) or not row["registered"]:
            return f"envelope refused: {code} names no registered theorem"
        if not all(isinstance(name, str) and name for name in row["registered"]):
            return (f"envelope refused: {code} names a registered theorem that is not a "
                    f"name ({row['registered']!r})")
        if not isinstance(row.get("contentless"), list) \
                or not all(isinstance(name, str) and name for name in row["contentless"]):
            return (f"envelope refused: {code} carries no contentless findings "
                    f"({row.get('contentless')!r})")
        seen.append(code)
    if seen != sorted(set(seen)):
        return f"envelope refused: statuses must be sorted by code and free of duplicates ({seen!r})"
    silent = [code for code in known if code not in seen]
    if silent:
        return ("envelope refused: the certificate reports no status for "
                f"{', '.join(silent)}, so it would leave part of the coverage "
                "unstated")

    artifacts = cert.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACT_NAMES):
        return (f"envelope refused: artifacts is not one entry per formal "
                f"artifact ({sorted(artifacts) if isinstance(artifacts, dict) else artifacts!r})")
    for name in ARTIFACT_NAMES:
        entry = artifacts[name]
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
            return f"envelope refused: artifact {name} names no path ({entry!r})"
        if not _HEX64.match(str(entry.get("digest"))):
            return f"envelope refused: artifact {name} carries no sha256 digest ({entry.get('digest')!r})"

    caveats = cert.get("caveats")
    if not isinstance(caveats, list) or not all(isinstance(line, str) and line for line in caveats):
        return f"envelope refused: caveats is not a list of strings ({caveats!r})"

    requirements = cert.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        return f"envelope refused: requirements is not a non-empty list ({requirements!r})"
    for requirement in requirements:
        if not isinstance(requirement, dict):
            return f"envelope refused: a requirement is not an object ({requirement!r})"
        if requirement.get("kind") not in REQUIREMENT_KINDS:
            return (f"envelope refused: requirement kind {requirement.get('kind')!r} is "
                    "not one this verifier re-derives")
        for member in ("subject", "source", "detail", "check"):
            if not isinstance(requirement.get(member), str) or not requirement[member]:
                return (f"envelope refused: requirement {requirement.get('kind')!r} has no "
                        f"{member} ({requirement.get(member)!r})")
    if not any(row["kind"] == REQ_GUARANTEE for row in requirements):
        return "envelope refused: no per-guarantee status requirement, so the certificate carries no coverage"

    checker = cert.get("checker")
    if not isinstance(checker, dict) or set(checker) != {"compiler", "ruleset"}:
        return ("envelope refused: checker is not the compiler version and the "
                "ruleset digest "
                f"({sorted(checker) if isinstance(checker, dict) else checker!r})")
    if not isinstance(checker.get("compiler"), str) or not checker["compiler"]:
        return f"envelope refused: checker.compiler is not a version ({checker.get('compiler')!r})"
    if not _HEX64.match(str(checker.get("ruleset"))):
        return f"envelope refused: checker.ruleset is not a sha256 ruleset digest ({checker.get('ruleset')!r})"
    signer = cert.get("signer")
    if signer is not None and not isinstance(signer, str):
        return f"envelope refused: signer is not a label ({signer!r})"
    return ""


def _requirement_key(requirement: dict) -> tuple[str, str, str]:
    return (requirement["kind"], requirement["subject"], requirement["source"])


#: The members of a per-guarantee row that are re-derived from `STATUS.md`,
#: the registry and the gate. `code` is the key, so it is not here.
STATUS_ROW_MEMBERS = ("name", "status", "status_cell", "theorems_cell",
                      "oracle_cell", "gap", "registered", "contentless")

#: The requirement members re-derived and compared. `kind`/`subject`/`source`
#: are the key, and `check` is what the certificate says produced the evidence,
#: which is as much of a claim as the evidence's own text.
REQUIREMENT_MEMBERS = ("detail", "check")


def affirm_key_id(named, key: bytes) -> str:
    """Refuse a certificate whose `key_id` is not the fingerprint of the key it
    is being checked with. Returns a reason, or `""`.

    The fingerprint is a function of the key alone, so a certificate that names
    a different one is a certificate about a different signer, even when its
    MAC is right: `key_id` is the member a reader uses to decide WHICH key to
    fetch, and a MAC that verifies under a key the record does not name is the
    one arrangement in which that decision cannot be made."""
    expected = attest.key_id(bytes(key))
    if named == expected:
        return ""
    return ("identity mismatch: the certificate names key "
            f"{str(named)[:64]!r}, which is not the key it is being checked "
            f"with (this key is {expected})")


def _read_named_source(named: str, root: Path) -> bytes | None:
    """The bytes of the source file a certificate names, when this machine can
    read them; `None` when it cannot.

    `None` is not a failure. A certificate travels, and the machine asked to
    check one need not hold the composition it is about; what it must not do is
    claim to have re-derived a digest it never read. A relative name is
    resolved against the working directory and then against the checkout that
    holds the formal package, never guessed at by basename alone: a wrong file
    compared against a right digest would be a refusal for the wrong reason."""
    path = Path(named)
    candidates = [path] if path.is_absolute() else [path, root.parent / path]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.read_bytes()
        except OSError:
            return None
    return None


def _brief(value) -> str:
    text = str(value)
    return text if len(text) <= 96 else text[:93] + "..."


def _recorded_not_derived(unverified: list[str]) -> str:
    """The tail a successful verification carries: which signed members this
    run read rather than re-derived. Silence here would let a green check be
    read as a claim about a member nothing on this machine can check."""
    if not unverified:
        return ""
    return "; recorded rather than re-derived here: " + ", ".join(unverified)


def verify_certificate(cert: dict, key: bytes, *, against: dict | None = None,
                       formal: str | Path | None = None) -> tuple[bool, str]:
    """Check a component certificate with `key`, re-deriving its evidence.

    Four independent failure modes, reported distinctly:

      * **signature mismatch**: the HMAC over the payload does not match. The
        key is wrong, or a member was altered after signing. Checked first,
        because it proves the record is the record before its contents mean
        anything. The MAC is domain separated, so an attestation signed with
        this key fails here rather than passing as a certificate.
      * **envelope refused**: the record is authentic but is not a certificate
        of the shape this verifier accepts, or claims a guarantee code or an
        evidence kind that does not exist.
      * **evidence mismatch**: the formal artifacts on this machine no longer
        re-derive the signed members. This is the check that makes the
        certificate proof carrying rather than self describing: the statuses
        are computed from `STATUS.md`, the registry and the gate, and a
        document whose rows moved makes the certificate fail rather than
        restate whatever it was signed with. The members re-derived and
        compared are the artifact digests, the whole of every per-guarantee
        row (its status, the cell that status is read out of, the cell's own
        name, the theorem names and the contentless findings), the caveats, the
        requirements with the check recorded behind each one, the proof model
        pins, the checker identity, the subject's source digest when the file
        it names is readable here, and the commit it names as a revision of
        this history.
      * **hash mismatch**: only when `against` is supplied, the composition
        presented now hashes differently from the one the certificate was
        signed for.
      * **identity mismatch**: the certificate names a key fingerprint that is
        not the key it is being checked with.

    A missing key, a non-object record, a record with no signature, or one
    whose artifacts cannot be read is a refusal, never silently "valid".

    Two signed members are inside the MAC and are NOT re-derived, because they
    are statements about the signing EVENT rather than about the tree:
    `timestamp` (`UNVERIFIABLE`) and `signer`. Every successful verification
    names them, and names any member this run could not reach (a source file
    this machine does not hold, a formal package that is not a checkout), so a
    green result is never read as a claim about what it did not check.
    """
    if not isinstance(key, (bytes, bytearray)) or not key:
        return False, "no signing key provided"
    if not isinstance(cert, dict):
        return False, "certificate is not an object"
    given_sig = cert.get(attest.SIGNATURE_FIELD)
    if not isinstance(given_sig, str):
        return False, "certificate has no signature"
    if not given_sig.isascii():
        # A peer-supplied record must not be able to raise here: comparing two
        # strings that are not both ASCII is a `TypeError` in
        # `hmac.compare_digest`, and a hostile certificate's failure mode is a
        # reason rather than a traceback. The shared predicate keeps this wording
        # and `--verify`'s in step.
        return False, ("certificate " +
                       attest.signature_not_ascii_reason(given_sig))
    if cert.get("kind") != CERT_KIND:
        return False, (f"not a component certificate: kind is "
                       f"{cert.get('kind')!r}, not {CERT_KIND!r}")

    try:
        expected_sig = _sign(cert, bytes(key))
    except NotCanonicalizable as error:
        return False, f"certificate cannot be verified: {error}"
    if not hmac.compare_digest(expected_sig, given_sig):
        return False, ("signature mismatch: wrong key, or the certificate was "
                       "tampered with after signing")

    # The fingerprint is part of the signed body and is a function of the key
    # alone, so there is no reason for it to be anything but the key's.
    named_key = affirm_key_id(cert.get("key_id"), key)
    if named_key:
        return False, named_key

    envelope = _validate_envelope(cert)
    if envelope:
        return False, envelope

    subject = cert.get("subject")
    if not isinstance(subject, dict) or "composition_hash" not in subject:
        return False, "certificate is missing subject.composition_hash"

    if against is not None:
        try:
            recomputed = attest.canonical_hash(against)
        except NotCanonicalizable as error:
            return False, f"the composition presented cannot be hashed: {error}"
        if not hmac.compare_digest(recomputed, subject["composition_hash"]):
            return False, ("hash mismatch: the composition changed since it was "
                           f"certified (certified {subject['composition_hash'][:12]}, "
                           f"now {recomputed[:12]})")

    try:
        root = formal_root(formal)
        live = formal_state(root, guarantees=attest.catalogued_guarantees())
    except CertError as error:
        return False, f"evidence unavailable: {error}"

    for name in ARTIFACT_NAMES:
        signed_artifact = cert["artifacts"][name]
        live_artifact = live["artifacts"][name]
        if not hmac.compare_digest(str(signed_artifact["digest"]),
                                   str(live_artifact["digest"])):
            return False, (f"artifact mismatch: {live_artifact['path']} on this "
                           "machine is not the revision this certificate was "
                           f"signed over (signed {str(signed_artifact['digest'])[:12]}, "
                           f"now {str(live_artifact['digest'])[:12]})")

    # Members this run could not re-derive, named in the result. A green check
    # is a claim about what was checked, so it says what was not.
    unverified = list(UNVERIFIABLE)

    source_bytes = _read_named_source(subject["filename"], root)
    if source_bytes is None:
        unverified.append("subject.source_hash")
    else:
        found = sha256_bytes(source_bytes)
        if not hmac.compare_digest(found, str(subject["source_hash"])):
            return False, ("subject mismatch: the file this certificate names "
                           f"({subject['filename']}) hashes to {found[:12]} here, "
                           f"but the certificate signs "
                           f"{str(subject['source_hash'])[:12]}: the composition "
                           "it speaks about is not the one on this machine")

    signed_commit = cert["as_of_commit"]
    if signed_commit is not None:
        contained, why = commit_in_history(root, str(signed_commit))
        if contained is False:
            return False, ("evidence mismatch: the certificate names commit "
                           f"{str(signed_commit)[:12]}, but {why}")
        if contained is None:
            unverified.append("as_of_commit")

    for member in ("lean_toolchain", "manifest_digest"):
        signed_member = cert["proof_model"][member]
        live_member = live["proof_model"][member]
        if signed_member != live_member:
            return False, ("evidence mismatch: the signed proof model member "
                           f"{member} is {_brief(signed_member)}, and the formal "
                           f"package pins {_brief(live_member)}")

    # The identity of the frontend that produced the verdict. Both members are
    # derived on this machine from the shipped ruleset and the running version,
    # so a certificate is checked by the build that speaks for it.
    identity = attest.checker_identity()
    for member in ("compiler", "ruleset"):
        if cert["checker"][member] != identity[member]:
            return False, ("checker mismatch: this certificate was signed by a "
                           f"different revl frontend (signed {member} "
                           f"{_brief(cert['checker'][member])}, this verifier is "
                           f"{_brief(identity[member])})")

    signed = {_requirement_key(row): row for row in cert["requirements"]}
    current = {_requirement_key(row): row for row in live["requirements"]}
    gone = sorted(set(signed) - set(current))
    new = sorted(set(current) - set(signed))
    moved = sorted(key for key in set(signed) & set(current)
                   if any(signed[key][member] != current[key][member]
                          for member in REQUIREMENT_MEMBERS))
    if gone or new or moved:
        parts = []
        if moved:
            members = sorted({member for key in moved
                              for member in REQUIREMENT_MEMBERS
                              if signed[key][member] != current[key][member]})
            parts.append(f"{len(moved)} requirement(s) changed "
                         f"({', '.join(key[1] for key in moved[:4])}; "
                         f"{', '.join(members)})")
        if gone:
            parts.append(f"{len(gone)} no longer recorded ({', '.join(key[1] for key in gone[:4])})")
        if new:
            parts.append(f"{len(new)} new evidence the certificate does not carry ({', '.join(key[1] for key in new[:4])})")
        return False, ("evidence mismatch: what this certificate carries and "
                       "what the formal artifacts record do not agree: "
                       + "; ".join(parts))

    # The status of every row first, so the message a reader has always been
    # given for a promoted or demoted guarantee is unchanged, then the rest of
    # each row: the prose a status is read out of, the theorem names, the
    # contentless findings. A row's status is derived FROM its cell, so a
    # certificate that kept the cell and changed the status, or kept the status
    # and changed the cell, is a certificate whose own members disagree.
    signed_status = {row["code"]: row["status"] for row in cert["statuses"]}
    live_status = {row["code"]: row["status"] for row in live["statuses"]}
    if signed_status != live_status:
        changed = sorted(code for code in set(signed_status) | set(live_status)
                         if signed_status.get(code) != live_status.get(code))
        return False, ("evidence mismatch: the recorded per-guarantee status "
                       f"changed for {', '.join(changed)}")

    live_rows = {row["code"]: row for row in live["statuses"]}
    for row in cert["statuses"]:
        derived = live_rows[row["code"]]
        for member in STATUS_ROW_MEMBERS:
            if row[member] == derived[member]:
                continue
            return False, ("evidence mismatch: the signed row for "
                           f"{row['code']} records {member} {_brief(row[member])}, "
                           f"and {row['code']} re-derives from the artifacts as "
                           f"{_brief(derived[member])}")

    signed_caveats = cert["caveats"]
    if signed_caveats != live["caveats"]:
        dropped = [line for line in live["caveats"] if line not in signed_caveats]
        added = [line for line in signed_caveats if line not in live["caveats"]]
        parts = []
        if dropped:
            parts.append(f"{len(dropped)} caveat(s) the artifacts record are "
                         f"missing (first: {dropped[0][:72]})")
        if added:
            parts.append(f"{len(added)} caveat(s) the artifacts do not record "
                         f"(first: {added[0][:72]})")
        return False, ("evidence mismatch: the signed caveats do not match what "
                       "the formal artifacts record: " + "; ".join(parts))

    return True, ("valid: certificate is authentic, its evidence re-derives "
                  "from the formal artifacts, and the composition matches"
                  if against is not None else
                  "valid: certificate is authentic and its evidence re-derives "
                  "from the formal artifacts") + _recorded_not_derived(unverified)


# --- rendering ------------------------------------------------------------- #
def render_certificate(cert: dict) -> str:
    """Human-readable form of a certificate (the default `revl attest
    --certificate` output; the full record is under `--json`)."""
    subject = cert.get("subject") or {}
    model = cert.get("proof_model") or {}
    lines = [
        f"certificate: {cert.get('verdict', '?')}  ({cert.get('kind')} v{cert.get('version')})",
        f"  subject:   {subject.get('filename', '?')}  "
        f"(source {str(subject.get('source_hash'))[:12]}, "
        f"ir {str(subject.get('composition_hash'))[:12]})",
        f"  proof:     {model.get('lean_toolchain', '?')}  "
        f"(as of {str(cert.get('as_of_commit'))[:12]})",
        f"  checker:   revl {(cert.get('checker') or {}).get('compiler', '?')}"
        f", ruleset {str((cert.get('checker') or {}).get('ruleset', '?'))[:12]}",
        f"  signed:    {cert.get('timestamp', '?')}  "
        f"({cert.get('sign_alg', '?')}, key {cert.get('key_id', '?')})",
        f"  evidence:  {len(cert.get('requirements') or [])} requirements over "
        f"{len(cert.get('statuses') or [])} guarantees",
    ]
    if cert.get("signer"):
        lines.append(f"  signer:    {cert['signer']}")
    lines.append("  coverage:")
    for row in cert.get("statuses") or []:
        lines.append(f"    {row.get('code', '?')} {row.get('status', '?'):8} "
                     f"{row.get('status_cell', '')}")
    if cert.get("caveats"):
        lines.append("  caveats:")
        for caveat in cert["caveats"]:
            lines.append(f"    {caveat}")
    lines.append(f"  signature: {cert.get('signature', '?')}")
    return "\n".join(lines)


def render_verify(ok: bool, reason: str, cert: dict) -> str:
    """Human-readable form of a certificate verification result. The reason
    always names what was checked, so a reader never has to guess whether a
    failure was the key, the envelope, or the evidence."""
    subject = cert.get("subject") or {}
    headline = "VALID" if ok else "INVALID"
    return (f"certificate: {headline}: {reason}\n"
            f"  subject: {subject.get('filename', '?')}  ir "
            f"{str(subject.get('composition_hash', '?'))[:12]}")
