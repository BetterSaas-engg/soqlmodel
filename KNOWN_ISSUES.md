# Known issues

## Generated output is not `ruff format`-clean (2026-08-27)

D14 enforces `ruff format` on this repo, and the rule it implies is that
`generate` must emit source that is already formatted — otherwise a user
whose repo runs a formatter gets a diff on every `soqlmodel generate`
with no schema change behind it. Exactly the phantom-diff failure the
project exists to prevent, one layer out from D13's BOM.

The generator does not wrap. A field name over roughly 30 characters
pushes its line past `line-length = 100`:

    Commission_Attainment_Rolling_12M__c: Field[str] = Field("Commission_Attainment_Rolling_12M__c", "picklist")

110 characters, from a real object in a real org. `HiBob_Variable_Pay_Synced_At__c`
lands on exactly 100, so the boundary is not theoretical either. Long
API names are the norm in Salesforce, not the exception — assume most
real orgs hit this.

It passes `ruff check` today only because `line-length` alone does not
enable E501. That is not reassurance; it is why nobody noticed.

Not fixed here: the generator has to learn to wrap, `write_combined_module`
needs a formatting-stability test against long names, and both are their
own ticket rather than something to slip into a review commit. Estimated
small — the emitter is one f-string.

## RESOLVED — mypy tests failing locally (2026-08-09, cleared 2026-08-26)

All five run and pass on this machine again, under mypy 2.3.0
(compiled), and `uv run python -m mypy src/soqlmodel` reports "Success:
no issues found in 11 source files". The Application Control policy
below no longer blocks the extension.

Kept rather than deleted, for two reasons. The block was environmental
and can come back, so the symptom is worth recognising. And the
reasoning in it still stands: if these ever fail to *run* again, they
still must not be skipped. `pytest.exe` may still be blocked — `uv run
python -m pytest` remains the way to run the suite.

CI on a Linux runner (SFM-11) is still where the type-checking claim
gets verified for real, since one machine's toolchain is not evidence
about anyone else's.

The original entry follows.

---

## mypy tests failing locally (2026-08-09)

Five tests that shell out to mypy fail with "DLL load failed while
importing mypy: An Application Control policy has blocked this file."
This is a Windows Application Control policy on the development
machine blocking mypy's compiled extension, not a code defect - the
same tests passed earlier in the session and ruff still runs clean.

The tests are deliberately NOT skipped. They verify that generated
code type-checks, which is this project's central contract; a
skip-on-unavailable would hide exactly the signal they exist to
provide.

Resolution: run them on a machine without the policy, or in CI
(SFM-11). Do not trust the type-checking claim until they pass.

The affected tests:

- `tests/test_fields.py::test_mypy_passes_clean_on_the_module`
- `tests/test_generate.py::test_generated_output_passes_mypy`
- `tests/test_generate.py::test_generated_fields_are_statically_resolvable`
- `tests/test_project.py::test_the_generated_module_type_checks`
- `tests/test_query.py::test_mypy_passes_clean_on_the_module`

`pytest.exe` is blocked by the same policy; run the suite with
`uv run python -m pytest`.

## Carry into SFM-11 (README, CI, PyPI release)

Not defects — decisions and staleness already identified, parked here so
SFM-11 does not have to rediscover them.

- **CLAUDE.md's "Current state" is stale.** It still reads "Skeleton
  only — `src/soqlmodel/__init__.py` and a smoke test." SFM-1..9 are
  done. Update it alongside the README, in the same pass.

- **CI must verify mypy on Linux regardless of it working locally
  again.** The RESOLVED entry above records the local block clearing on
  2026-08-26. That is one machine's toolchain and is not evidence about
  anyone else's; the type-checking claim is this project's central
  contract and CI is where it gets verified for real.

- **CI runs `ruff format --check` beside `ruff check`** (D14).

- **Generated output is not format-clean** — see the first entry in this
  file. Decide whether it blocks the PyPI release or ships as a known
  limitation.

- **`generate.py` has a dangling "see the backlog" reference** in the
  comment above `FALLBACK_TYPE`, about compound types (address,
  location) falling back to `Any`. There is no backlog file. Either
  write one or make the comment self-contained.
