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