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

v1 scope is complete: SFM-1 through SFM-11. The four stages above are
implemented, plus drift detection and execution.

| Module | What it does |
|---|---|
| `extract.py` | Stage 1. Shells out to `sf`; the only module that touches the org. Also owns `write_snapshot` / `read_snapshot` — the format rules live together. |
| `describe.py` | Stage 2. Trims a describe payload into a deterministic snapshot. |
| `generate.py` | Stage 3. Snapshot → Python source. Pure. |
| `fields.py` / `query.py` | Stage 4. `Field`, `Condition`, and the SOQL builder. Pure, no network. |
| `check.py` | Diffs a committed snapshot against the org. CRITICAL/WARNING (D10). |
| `execute.py` | Hands rendered SOQL to a caller-supplied client and drains the cursor (D15). |
| `project.py` | Orchestration over the stages. Owns no classification logic. |
| `config.py` | `soqlmodel.toml` — the declared dependency (D9). |
| `errors.py` | `SoqlModelError` and one subclass per stage (D11). |
| `cli.py` | `snapshot`, `generate`, `check`. Thin. |

386 tests. CI runs ruff, `mypy --strict`, and the suite across Python
3.11–3.14 on Linux.

**Not done, and deliberately so:** generated output is not `ruff format`-clean
(long field names overflow 100 chars); the simple-salesforce execution path has
never run against a live org; no license is chosen yet. All three are in
`KNOWN_ISSUES.md`, and the last two block a PyPI release.

## Errors

Two hierarchies, and the distinction is load-bearing (D11).

`SoqlModelError` and its subclasses are **user failures** — bad config,
unreachable org, a declared field the org lacks. The CLI catches these and
prints one line, exit 2.

Plain `ValueError` / `TypeError` are **programming failures** — a caller
misusing the library. The CLI does not catch them, so they surface as
tracebacks. Nothing in `errors.py` subclasses `ValueError`; that is what stops
`except SoqlModelError` from swallowing a bug. When adding a raise site, decide
which kind it is before picking a class.
