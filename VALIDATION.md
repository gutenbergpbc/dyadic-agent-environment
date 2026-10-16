# Release validation — 2026-10-15

- `uv sync --extra test --locked`: passed in a fresh environment (CPython 3.13.5, macOS arm64).
- `python -m pytest -q`: **54 passed**. The first sandboxed attempt passed 53 tests and was blocked from writing Inspect’s host trace log; the complete rerun with that filesystem access passed.
- `python -m workplace.runner prepare runs/preparation`: passed, producing configuration, assignments, contradiction and source-hash files without model calls.
- Extracted environment source matches the upstream revision in `SOURCE.json`.
- The published figure data is byte-identical to the previously reviewed snapshot. Only page wording and links changed.
- Checked the publication file set for credentials, private document links, internal storage references and local personal paths; none found. Run output, virtual environments and internal adapters are excluded.

## Not exercised for this release

The Docker daemon was not running on the packaging machine. Docker validation and the runtime canary were therefore **not run** during this extraction. The README includes the required no-model Docker validation before a live batch can start. No paid model calls were made. Unit tests and preparation do not establish a successful end-to-end Docker run.

The public release is an environment snapshot, not an exact reproduction package for all historical study batches; the internal collection coordinator and urgency launcher are excluded.
