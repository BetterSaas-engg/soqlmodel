# soqlmodel

Generates typed Python models from a Salesforce org's schema, builds SOQL through
them, and detects schema drift.

The point: make a data pipeline's dependency on a Salesforce org explicit,
versioned, and checkable — so a schema change surfaces as a failing build rather
than as wrong numbers.

## Architecture

Four stages, connected by files:

1. **Extract** — pull a describe payload from the org via the `sf` CLI.
2. **Snapshot** — trim the payload into a stable, diffable snapshot JSON.
3. **Generate** — emit typed models from that snapshot.
4. **Query** — build SOQL strings from the models.

Stages 2–4 are pure functions with no network access. Only stage 1 touches the
org. The snapshot file is the seam: everything downstream reads the file, never
the org.

## Constraints

- `src/` layout; the package is `src/soqlmodel`.
- **Generated models are plain classes with explicit annotated attributes.** No
  metaclasses, no `__getattr__`, no runtime attribute magic. If mypy and Pylance
  can't see every field statically, the product doesn't work.
- **Snapshot output must be deterministic** — no timestamps, sorted keys. A
  re-extract against an unchanged org must produce a byte-identical file.
- **Never commit anything from `.scratch/`.** It holds real org schemas. It is
  gitignored; keep it that way.
- Read [DECISIONS.md](DECISIONS.md) before proposing architectural changes. Append
  to it, don't rewrite it.

## Toolchain

`uv` for env and deps, `pytest` for tests, `ruff` for lint/format (line length
100). Python >= 3.10.

```powershell
uv sync
uv run pytest
uv run ruff check .
```

## Current state

Skeleton only — `src/soqlmodel/__init__.py` and a smoke test. The stages above
are the target design, not yet implemented.
