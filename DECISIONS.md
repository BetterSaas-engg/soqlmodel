# Decisions

Design decisions and why. Append, don't rewrite.

## D1 — simple-salesforce for execution (2026-08-09)

**Context:** We need to send SOQL to Salesforce and get rows back.

**Decision:** Depend on simple-salesforce. We do not implement auth,
HTTP, pagination, or retries.

**Consequence:** We inherit its limitations and its release cadence.
Accepted — reimplementing auth badly is how this project dies.
Contained to one module, so it is reversible.

## D2 — sf CLI for schema extraction in v1 (2026-08-09)

**Context:** We need an org's describe payload. Either the library
authenticates itself, or we shell out to the already-authenticated
`sf` CLI.

**Decision:** Shell out to `sf` for v1.

**Consequence:** Users need the Salesforce CLI installed — fine for
consultants, awkward in a container. Accepted because the schema JSON
file is a seam: everything downstream reads the file, not the org, so
a credential-based fetcher can be added later without touching the
generator, query builder, or check.

## D3 — Store 16 of 57 describe properties (2026-08-09)

**Context:** Salesforce returns 57 properties per field. Storing all
of them makes drift diffs unreadable; storing too few makes drift
undetectable.

**Decision:** Store: name, label, type, nillable, filterable, sortable,
referenceTo, relationshipName, deprecatedAndHidden, custom, calculated,
length, precision, scale, picklistValues, restrictedPicklist.

Picklist values are stored in the snapshot but NOT rendered into
generated types. Values are sorted so diffs are stable.

**Consequence:** Picklist changes are detected by `check` without making
the models churn every time an admin adds a value. Removed/renamed
values are reported as critical (breaks mappings loudly); added values
as warnings (breaks mappings silently). Rejected `Literal[...]` types
in v1 for this reason — revisit if the noise proves tolerable.

## D4 — Store only active picklist values (2026-08-09)

**Context:** Salesforce marks picklist values active or inactive. We
flatten values to plain strings, so an inactive flag would not survive
into the snapshot.

**Decision:** Store only active values, flattened and sorted.

**Consequence:** Deactivating a value appears as a removal, which `check`
reports as CRITICAL — correct, since no new records will carry a
deactivated value, so a downstream mapping is stale either way.
We lose the ability to distinguish "deactivated" from "deleted".
Accepted: the downstream effect is identical.

## D5 — Snapshot readability: drop nulls, fold case, write UTF-8 (2026-08-09)

**Context:** Running the pipeline against a real org (Account, 239
fields) surfaced three things a hand-written fixture could not. Every
one of them costs a human reading a drift diff.

**Decision:** Three changes to the snapshot format.

1. `trim_field` omits properties whose value is `null`.
   `relationshipName` is null on 223 of 239 real Account fields — dead
   weight in every diff. Falsey-but-not-null values are kept:
   `nillable: false` and `length: 0` are real answers, not missing ones.
   This matches the existing omit-when-empty rule for `picklistValues`.

2. `build_snapshot` sorts case-insensitively, with the exact name as a
   tiebreaker: `key=lambda f: (f["name"].lower(), f["name"])`. Seven
   lowercase custom fields (`eCPM_EUR__c`, `ssp_link__c`, …) sorted to
   the bottom under raw ASCII ordering, away from their `__c` siblings.
   The tiebreaker keeps the order total, so names differing only by case
   do not depend on input order.

3. `write_snapshot` passes `ensure_ascii=False` *together with* an
   explicit `encoding="utf-8"`. Non-ASCII appears in real labels and
   picklist values; escaped as `\uXXXX` it is unreadable in a diff.
   The explicit encoding is not optional — `ensure_ascii=False` alone
   crashes on Windows, whose default codepage is not UTF-8.

**Consequence:** Snapshots are smaller and diffs read as prose. A field
*gaining* a relationship still shows as an added key, so change 1 loses
no drift signal. Changes 1 and 2 alter the bytes of every existing
snapshot: the first regeneration after this lands is a large diff that
is formatting, not drift. Regenerate deliberately, not during a review.
Determinism is unaffected — verified against the real org, two runs,
identical SHA-256.

## D6 — Snapshots carry a format_version (2026-08-09)

**Context:** D5 changed what we store, which changed the bytes of every
snapshot without any org having changed. `check` compares snapshots, so
it would have reported that reformatting as schema drift — the exact
false positive this project exists to prevent. D5 will not be the last
such change.

**Decision:** `build_snapshot` emits `format_version: 1` as the first
key. Bump it whenever a change to what we store alters the bytes of an
existing snapshot. `check` compares versions before comparing fields,
and on a mismatch tells the user to regenerate rather than reporting
drift.

**Consequence:** Adding this later is itself the change it protects
against, so it lands before any snapshot exists in the wild — today,
while the only snapshot is in a `.scratch/` directory. Cost is one line
per snapshot. Note `write_snapshot` sorts keys, so the *file* orders it
after `fields`; "first key" is about the dict, not the JSON.

Nameless fields are now rejected at the origin in `trim_field` rather
than sorted defensively: a field with no `name` is a corrupt payload,
and failing there beats emitting a nameless field for the generator to
choke on later.