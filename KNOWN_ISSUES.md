# Known issues

## The simple-salesforce leg has never run against a live org (2026-08-27)

**The single largest untested claim in v1.** D1 names simple-salesforce
as the execution dependency and `soqlmodel[salesforce]` installs it, but
no `simple_salesforce.Salesforce` object has ever been handed to
`soqlmodel.execute` against a real org. Not once.

What *is* proven, and it is not nothing:

- Paging against **real cursors**. SFM-10's acceptance run drained 4786
  live Contact rows from FULL Sandbox in three batches
  `[2000, 2000, 786]` — one `query`, two `query_more` — matching
  `COUNT()` exactly. Real `nextRecordsUrl` values, real batch
  boundaries, real 2000-row limit.
- But through a client backed by `sf api request rest`, not
  simple-salesforce. It satisfies the same Protocol; that is precisely
  why it could stand in.
- Protocol conformance to simple-salesforce is covered offline by
  `test_a_real_salesforce_shaped_object_satisfies_the_protocol`, which
  transcribes `Salesforce.query` and `Salesforce.query_more`'s real
  signatures and asserts `isinstance`.

So the gap is narrow but real: everything except *simple-salesforce
itself answering our two calls over the wire*. The transcribed
signatures are a copy, and a copy can go stale — if upstream renames a
parameter, that test keeps passing while real usage breaks.

Blocked on org auth, not on effort — see the next entry. Carried into
SFM-11: either CI closes it, or the README says plainly that the
documented client is unexercised.

## `sf org display` does not yield a usable session for a third-party client (2026-08-27)

Hit doing SFM-10's live acceptance run. The `accessToken` returned by
`sf org display --target-org "FULL Sandbox" --json` is rejected by every
authenticated REST endpoint (401 `INVALID_AUTH_HEADER` on
`/services/data/vNN.0/query`, 403 on `/services/oauth2/userinfo`), while
the sf CLI itself queries the org fine. So a `simple_salesforce.Salesforce`
built from that token cannot be used here.

A trap worth writing down: `GET /services/data/` returns **200 with a
deliberately bogus token**. It does not enforce auth, so it is useless as
a credential check and will tell you a dead token is alive. Verify
against `/services/data/vNN.0/limits` instead.

Consequence for acceptance runs: the drain was verified against the real
org through a client backed by `sf api request rest`, which uses the
CLI's own session and satisfies the same Protocol. That exercises the
cursor logic against real batches. The simple-salesforce leg
specifically is covered only by offline tests — including one that
transcribes its real method signatures and asserts Protocol conformance.

Resolving it means obtaining a session another way (a fresh `sf org
login`, or username/password/token credentials). Not attempted here:
that is credential material and it was not needed to prove the paging.

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

## CORRECTION — Application Control blocks some console scripts, not all (2026-08-27)

Reporting during SFM-9 and SFM-10 said the `soqlmodel` console script
was blocked by Application Control. That was wrong, and *how* it was
wrong is the useful part.

Git Bash prints `Permission denied` both for a binary the policy has
blocked and for one it merely cannot exec. The two are
indistinguishable from bash. PowerShell names the cause outright.

Measured 2026-08-27, same `.venv`, same session:

    pytest.exe     bash: Permission denied
                   PowerShell: "An Application Control policy has
                   blocked this file"          <- genuinely blocked

    soqlmodel.exe  bash: Permission denied
                   PowerShell: runs, exit 0    <- never blocked

    mypy.exe       PowerShell: runs, exit 0    <- no longer blocked

So the policy is real and `pytest.exe` really is blocked: the 2026-08-09
entry below is accurate and stays. What was overstated is the
generalisation from it — "console scripts are blocked" as a class, and
`soqlmodel.exe` in particular, which runs fine.

**Rule: never conclude "Application Control" from a Git Bash
`Permission denied`.** Re-test in PowerShell, which says so explicitly.
An overstated known issue is worse than none, because it teaches the
next reader to wave away the next real block.

`uv run python -m pytest` remains the way to run the suite — for
`pytest.exe`, that reason is real.

## RESOLVED — mypy tests failing locally (2026-08-09, cleared 2026-08-26)

All five run and pass on this machine again, under mypy 2.3.0
(compiled), and `uv run python -m mypy src/soqlmodel` reports "Success:
no issues found in 11 source files". The Application Control policy
below no longer blocks the extension.

Kept rather than deleted, for two reasons. The block was environmental
and can come back, so the symptom is worth recognising. And the
reasoning in it still stands: if these ever fail to *run* again, they
still must not be skipped. `pytest.exe` **is** still blocked, confirmed
2026-08-27 — `uv run python -m pytest` remains the way to run the suite.

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

- **Decide how the untested simple-salesforce leg is handled** — the
  first entry in this file, and the largest untested claim in v1.
  SFM-11 must land on one of these and say which:

  1. **CI closes it.** Needs org credentials as repository secrets and
     a live sandbox reachable from a runner. That makes CI depend on an
     org staying alive and a secret staying valid — a flaky-CI risk
     traded for a real guarantee, and it cannot run on a fork's PR.
  2. **A nightly or manual job closes it**, separate from PR CI, so a
     dead sandbox does not redden every pull request.
  3. **The README says so plainly.** No secrets, no flake, and the
     limitation is disclosed rather than hidden. Weakest guarantee.

  A cheap partial that is worth doing regardless of the choice: a CI
  job that installs `soqlmodel[salesforce]` and asserts
  `isinstance(Salesforce(...), SalesforceClient)` against the **real**
  imported class rather than our transcribed stand-in. No org needed,
  and it catches the specific way the offline test can rot — an
  upstream signature change that leaves the copy passing.

- **CLAUDE.md's "Current state" is stale.** It still reads "Skeleton
  only — `src/soqlmodel/__init__.py` and a smoke test." SFM-1..10 are
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
