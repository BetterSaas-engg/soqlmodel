# Known issues

## RELEASE STATUS — both D17 blockers cleared (2026-08-27)

1. **License: resolved.** MIT, `Copyright (c) 2026 OptimaCore`. `LICENSE`
   at the repo root, `license = "MIT"` and `license-files = ["LICENSE"]`
   in `pyproject.toml` (PEP 639, so no legacy `License ::` classifier —
   setting both is an error on modern backends). Verified in the built
   artifact, not just the repo: the wheel's METADATA carries
   `License-Expression: MIT` and `License-File: LICENSE`, and the file
   ships at `soqlmodel-0.1.0.dist-info/licenses/LICENSE`.

2. **Generated output stability: resolved** by D18. Long fields now wrap
   with a magic trailing comma, which no formatter collapses.
   Re-measured against the same three real objects (592 fields):
   `ruff format` leaves the output unchanged at 79, 88, 100 and 120, and
   no line exceeds the configured length at any of them. `line_length`
   is configurable in `soqlmodel.toml`, default 88.

**CI: green.** First real run 2026-08-28, all nine jobs passing on
Linux, zero warnings. It answered the one question nothing local
could: `soqlmodel[salesforce]` **does** resolve on Python 3.11 — the
ARM64 Windows `cryptography` failure was platform-specific, as
suspected. It also verified `mypy --strict` on 3.11-3.14 and that the
wheel carries `License-Expression: MIT`.

**Remaining before PyPI: credentials.** Nothing else.

**No PyPI credentials exist on this machine.** No `TWINE_*`,
`UV_PUBLISH_TOKEN` or `PYPI_TOKEN` env vars, no `~/.pypirc`, no keyring.
Any upload — TestPyPI included — needs a token supplied first.

**The name `soqlmodel` is free on PyPI** — `GET
https://pypi.org/pypi/soqlmodel/json` returns 404, re-checked
2026-08-27. A TestPyPI upload would *not* confirm this: separate
registries, separate namespaces. The 404 is the evidence, and it holds
only until someone else takes the name.

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

Blocked on org auth, not on effort — see the next entry.

**Narrowed substantially in SFM-11**, though not closed.
`tests/test_salesforce_conformance.py` now stubs `_call_salesforce` — the
one method that performs I/O — and lets the genuine
`Salesforce.query` and `Salesforce.query_more` run our drain. That
exercises their real parameter names, the `identifier_is_url` URL
branch, and response parsing, with no org and no secrets. CI runs it on
3.11 and 3.14 with `SOQLMODEL_REQUIRE_SALESFORCE_EXTRA=1`, which turns
the module's skip into a hard failure so the job cannot pass by running
nothing.

Why that shape and not the `isinstance` check originally planned:
**`issubclass` does not catch a renamed parameter.** A class declaring
`query(self, soql_text)` and `query_more(self, locator, as_url=False)`
passes `issubclass` against our runtime_checkable Protocol and would
break every call we make — verified in
`test_issubclass_alone_does_not_catch_renamed_parameters`. A conformance
check built on `isinstance` alone would have been theatre.

What is still unproven: those two methods against real Salesforce HTTP.
The README discloses it.

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

## RESOLVED — generated output was not `ruff format`-clean (2026-08-27)

Fixed the same day by D18; kept because the measurements are the
argument for the design and because the failure mode is worth
recognising if the emitter is ever changed.

The generator did not wrap. A field name over 32 characters pushed its
line past 100:

    Commission_Attainment_Rolling_12M__c: Field[str] = Field("Commission_Attainment_Rolling_12M__c", "picklist")

110 characters, from a real object. Across Account, Contact and
Opportunity unscoped — 592 fields — 18 lines exceeded 100. 3% of lines
but 100% of modules, since one long line dirties the file.

It passed `ruff check` only because `line-length` alone does not enable
E501. That was why nobody noticed, not a reason it was fine.

The fix is not "shorter lines" — see D18. A formatter joins as well as
splits, so the requirement is stability in both directions, and that is
what `tests/test_generated_format_stability.py` asserts against a real
`ruff format` at four line lengths.

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

## SFM-11 outcome, and what is left

The SFM-11 carry list is resolved. Recorded here so the next reader sees
the disposition rather than the original to-do.

**Done in SFM-11:**

- CI added (`.github/workflows/ci.yml`): ruff check + `ruff format
  --check` (D14); `mypy --strict src/soqlmodel` and the full suite across
  Python 3.11-3.14 on Linux; an explicit step re-running the five
  mypy-dependent tests by name so they cannot be silently skipped;
  simple-salesforce conformance with the extra installed; a job proving
  the package works with the extra *absent*; and a wheel build installed
  into a clean venv with a `py.typed` check.
- The 3.11-3.14 range is now tested, not merely claimed. It found two
  real breaks, both fixed: `__protocol_attrs__` does not exist before
  3.12, and a test guarded on `sys.modules` while the code branched on
  `find_spec`, so it failed whenever the extra was installed.
- README written. CLAUDE.md updated. Naming settled (D16). Release call
  made (D17).
- The simple-salesforce gap was narrowed by transport-stubbing rather
  than the planned `isinstance` check, which would not have caught what
  it was meant to catch. See that entry above.

**Left over:**

- **`generate.py` has a dangling "see the backlog" reference** in the
  comment above `FALLBACK_TYPE`, about compound types (address,
  location) falling back to `Any`. There is no backlog file. Either
  write one or make the comment self-contained.
- **CI has run and is green** (2026-08-28). Three findings on the way,
  all fixed: `actions/checkout@v4` and `setup-uv@v5` were on the
  deprecated Node 20; bumping them to "the latest release" broke every
  job because `setup-uv` publishes releases past v10 but no floating
  major tag beyond **v7**, so `@v10` did not resolve; and the license
  claim was only ever checked by hand, so the build job now asserts it
  in the wheel. Pin actions by floating major tag verified with `git
  ls-remote`, not by the version the releases API reports.
- **The release blockers at the top of this file.**
