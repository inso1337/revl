# Research drafts about revl

This directory holds research papers about revl, each at the stage it is in
today. Every one is a draft. None has been peer reviewed, and none should be
cited as a settled result.

Each draft is one file in `papers/`, named `<ID>-<slug>.md`. A draft opens with a
short block of metadata (id, title, theme, kind, maturity, a one-sentence status
and the drafts it is planned to be combined with), then the same ten sections:
Pitch, Abstract, Problem and claim, Contributions, Related work, Evidence on
main, Numbers that exist, Paper outline, Threats to validity and limits, and
Fact-check. The status line says what exists on main today and what kind of
result is still needed, and the maturity field is the short form of it:

- `shipped-evidence`: the mechanism is on main and the claim is backed by tests
  or measurements there.
- `partial`: part of the mechanism is on main; the rest, or the evaluation, is not.
- `design-only`: a written design; little or nothing runs yet.

`research.json` names the collection: author, licence, the repository snapshot,
and the list of themes.

## Snapshot

The drafts were written and checked against revl at commit `45bec431f`. Every
repository path in them (`path`, `path:line` or `path:start-end`) refers to that
commit. The site links each one to the file at that commit, so a reference keeps
its meaning while main moves on. The Fact-check section of each draft states how
many claims were checked against the snapshot and how many were corrected.

## Reading them

The drafts are published as a section of the project site:
<https://inso1337.github.io/revl/research/>. The index lists the drafts by theme
with their state and has a filter by theme, state and kind. Drafts that cover one
argument from several sides are listed as planned combined papers.

## Checked background sources

- [Cordis paper reading note](cordis-paper-reading.md) records the paper's
  theorem assumptions, capability boundary, and implementation-language
  wording.
- [Related-work table](related-work.md) records a bounded set of opened primary
  sources and the date each was checked. It is not an exhaustive novelty
  survey.

## Licence and author

Author: Thomas Moussajee. The text in this directory is licensed under the
Creative Commons Attribution 4.0 International license (CC BY 4.0),
<https://creativecommons.org/licenses/by/4.0/>. See `LICENSING.md` at the
repository root.

The date of the public commit that added a draft is the record of when it was first
published. The licence allows reuse, including commercial use, provided the author is credited.

## How to cite

To cite the collection:

```bibtex
@misc{revlresearch,
  author       = {Thomas Moussajee},
  title        = {Research drafts about revl},
  year         = {2026},
  howpublished = {\url{https://inso1337.github.io/revl/research/}},
  note         = {Collection of research drafts, not peer reviewed. Written against revl commit 45bec431f. Licensed CC BY 4.0.}
}
```

The page of each draft carries its own citation block.

## Render the site locally

The pages are rendered at deploy time and are not committed. The renderer uses
only the Python standard library:

```
python3 tools/publish_research.py --write    # renders into site/research/
python3 tools/publish_research.py --check    # fails if site/research/ has drifted
python3 tools/publish_research.py --list     # one line per draft
python3 -m http.server 8000 --directory site # then open http://localhost:8000/research/
```

The renderer refuses, and names the file, a draft that lacks a metadata key or a
section, that uses an unknown theme, kind or maturity, or that is planned to be
combined with a draft that does not exist.
