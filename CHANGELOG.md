# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Reasoning behind these changes lives in [DECISIONS.md](DECISIONS.md), which is
append-only; entries below cite the decision they came from.

## [Unreleased]

## [0.2.0] - 2026-09-04

The release that makes `soqlmodel` usable without the Salesforce CLI — in a
container, on a CI runner, or from a scheduler.

### Added

- **Credential-based schema extraction.** `snapshot` and `check` accept
  `--source credentials` and reach the org over REST via
  [simple-salesforce](https://pypi.org/project/simple-salesforce/), with no `sf`
  CLI involved. Requires the `[salesforce]` extra. Credentials come from four
  environment variables and only from there — never from `soqlmodel.toml`, which
  is a committed file:
  `SOQLMODEL_SF_USERNAME`, `SOQLMODEL_SF_CONSUMER_KEY`,
  `SOQLMODEL_SF_PRIVATEKEY_FILE`, `SOQLMODEL_SF_DOMAIN`.
  A missing variable or a missing extra is an error naming exactly what is
  absent, before any network call. (D21)
- **`--source sf|credentials`** on `snapshot` and `check`, defaulting to `sf`.
  The source is never inferred from whether credentials happen to be present in
  the environment: a secret sitting in a shell profile must not change how the
  tool talks to an org. Presence of a secret is not consent to use it. (D19/D21)
- `CredentialError`, so the exception type alone says which extraction source
  failed. (D11)
- A live test asserting both sources build byte-identical snapshots, opt-in
  behind `SOQLMODEL_LIVE_ORG=1` and not run by CI.

### Changed

- **BREAKING: `api_version` is now required in `soqlmodel.toml`** for `snapshot`
  and `check`. There is no default, and existing configs will fail with
  `no api_version configured` until they add one:

  ```toml
  api_version = "68.0"
  ```

  This is deliberate rather than an oversight. Left to themselves the `sf` CLI
  and simple-salesforce negotiate *different* API versions, and two describes at
  different versions return different **field lists** — which `check` reports as
  `field no longer exists in the org` at CRITICAL. That is a red build, blamed on
  the org, caused by nothing but which client asked. A default would only pick a
  winner between the skews while hiding the pin, so the day an org stopped
  serving that version the same false CRITICAL would return with no setting to
  point at. One line of config now beats an undiagnosable failure later. Both
  sources are pinned identically; `sf sobject describe` accepts `--api-version`.
  `generate` never touches an org and does not need it. (D21)
- **`org` is now a label.** It is still written to the snapshot as before, and on
  the `sf` source it still doubles as the `--target-org` alias, so existing
  configs keep working. On the credential source it is only a name — there is no
  alias to be. Safe because drift comparison reads `format_version` and `fields`
  and never `org`, so a snapshot taken through one source and checked through the
  other cannot report drift because the label differs. (D21)
- `no org configured` no longer calls `org` an alias, which sent readers looking
  for an `sf` org that need not exist.

### Fixed

- **Snapshots were corrupted on any machine whose default encoding is not UTF-8,
  and the corruption caused false CRITICAL drift.** `fetch_describe` decoded the
  CLI's stdout with `locale.getpreferredencoding()` — cp1252 on a stock Windows
  install — while `sf --json` emits UTF-8 on every platform. Every multi-byte
  character was mangled on the way into the snapshot: U+0421 CYRILLIC CAPITAL
  ES (bytes `D0 A1`) arrived as `Ð¡`.

  Two consequences, both worse than the cause. Snapshots stopped being
  deterministic across platforms, so a Windows developer and a Linux runner
  produced different bytes from the same unchanged org — breaking a guarantee the
  README states. And a snapshot committed from one and checked from the other
  left `was - now` non-empty, raising `value removed "..."` at CRITICAL: a
  failing build reporting drift that never happened, which attacks the premise
  that a failing build means something.

  CI could not catch it — Linux runners are UTF-8 — and no test crossed the
  subprocess boundary, so the suite was green throughout. Now decoded as UTF-8
  explicitly, with a test whose fake decoder is pinned to cp1252 rather than
  reading the live locale, so it fails on a UTF-8 runner too. (D20)
- Bad credentials on `--source credentials` now exit 2 with one line instead of
  a traceback. simple-salesforce's `SalesforceAuthenticationFailed` is neither a
  `SoqlModelError` nor an `OSError`, so the CLI's handler missed it and the most
  ordinary mistake this feature has looked like a crash. Converted to
  `AuthenticationError` at the boundary; its message carries simple-salesforce's
  own text, which contains no credential values. (D11)
- Every file and subprocess boundary now names its encoding explicitly rather
  than inheriting the reader's. The others were already correct; this records the
  rule and adds the missing write/read pairing test for non-ASCII. (D20)

## [0.1.0] - 2026-08-28

Initial release. Generates typed Python models from a Salesforce org's schema,
builds SOQL through them, and detects schema drift.

- Four stages connected by a committed snapshot file: extract via the `sf` CLI,
  trim to a deterministic snapshot, generate typed models, build SOQL. Stages 2–4
  are pure and never touch the org. (D2)
- `soqlmodel snapshot`, `generate` and `check`; `check` exits 1 on CRITICAL
  drift so a schema change fails the build instead of producing wrong numbers.
  (D10)
- Generated models are plain classes with explicit annotated attributes — no
  metaclasses, no `__getattr__` — so mypy and Pylance resolve every field
  statically.
- Zero runtime dependencies. `simple_salesforce` is an optional extra used only
  to execute queries, and is typed structurally so nothing imports it. (D1/D15)
- MIT licensed.

[Unreleased]: https://github.com/BetterSaas-engg/soqlmodel/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/BetterSaas-engg/soqlmodel/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/BetterSaas-engg/soqlmodel/releases/tag/v0.1.0
