# Upstream fix draft — cordis-py: make `hmr.py`'s watchdog fallback reachable

> **Status: DRAFT / BLOCKED.** `inso1337/cordis-py` has issues disabled (the
> API answers `410 Gone` for issue creation), so this note is the record
> instead of a filed issue. The defect is pinned in-repo by
> `tests/test_cordis_wheel_declares_its_deps_946.py::test_the_zero_dependency_install_upstream_documents_cannot_work_yet`,
> which fails the day the fix below lands — that failure is the signal to move
> `watchdog` out of `site/build.py`'s hard requirements and back under the
> `hmr` extra.
>
> Found while fixing [#946](https://github.com/inso1337/revl/issues/946): the
> published `cordis` wheel declared no dependencies at all. Half of that was
> the wheel's own metadata (`site/build.py`), fixed there; this is the other
> half, and it is not in this repository.

---

## The defect

`cordis/hmr.py` is written to degrade gracefully, and the degradation is
implemented — and unreachable:

```
:28  try:
:29      from watchdog.events import FileSystemEventHandler
:30      from watchdog.observers import Observer
:31  except ImportError:
:32      FileSystemEventHandler = None
:33      Observer = None
:35  WATCHDOG_AVAILABLE = Observer is not None
:40  class _WatchHandler(FileSystemEventHandler):     # <- executes unconditionally
...
:84      if WATCHDOG_AVAILABLE:
:85          self._start_observer()
:86      else:
:87          self._dispose_timer = self.ctx.setInterval(self._poll, ...)
```

The `else` at `:86-87` is the mtime-polling fallback the module docstring
promises ("an mtime-polling fallback so the zero-dependency installation keeps
working", naming the extra `cordis[hmr]`). The class statement at `:40` runs at
import time with `FileSystemEventHandler = None`, so the module never finishes
importing:

```
TypeError: NoneType takes no arguments
```

`WATCHDOG_AVAILABLE` cannot be read in exactly the case it was written for, and
the `else` branch is dead. `cordis/__init__.py` imports `.hmr` at module scope,
so this is not an `import cordis.hmr` problem — it is `import cordis`, and it
names neither watchdog nor a fix.

The comment at `:31` says `# pragma: no cover — exercised by the polling tests`.
Those tests cannot be exercising this path, because the module raises before any
test body runs: the coverage story is wrong in the same way the fallback is.

## Repro

```sh
python -m venv /tmp/c
/tmp/c/bin/python -m pip install pyyaml
/tmp/c/bin/python -c "import cordis"     # TypeError: NoneType takes no arguments
/tmp/c/bin/python -m pip install watchdog
/tmp/c/bin/python -c "import cordis"     # OK
```

Measured against `site/vendor/cordis-4.0.0-py3-none-any.whl` (the wheel revl
builds from the clone at the `harden-fiber-lifecycle` pin) and against
`cordis` 4.0.0 from PyPI — identical.

## Draft patch

```python
if WATCHDOG_AVAILABLE:
    class _WatchHandler(FileSystemEventHandler):
        ...  # unchanged
else:
    class _WatchHandler:
        def __init__(self, hmr, is_ignored):
            raise RuntimeError("watchdog is not installed")
```

and drop `# pragma: no cover` from the `except ImportError` branch so the
polling path is actually measured. With that, `watchdog` becomes a genuine
optional extra, `cordis[hmr]` means what the docstring says, and the
zero-dependency install works.

## What revl does meanwhile

`site/build.py` declares `pyyaml` **and** `watchdog` as hard requirements,
because until this lands a wheel that declares watchdog as an extra installs
cleanly and then dies on `import cordis` — the bug it is meant to fix. The
browser path is unaffected either way: `site/js/playground.js` installs the
wheel with `deps=False`, takes `pyyaml` from the Pyodide distribution by name,
and shims `watchdog` inertly (there is no filesystem to watch).
