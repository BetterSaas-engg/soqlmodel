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

## D7 — Comparison operators build filters, not booleans (2026-08-09)

**Context:** A query builder needs `Account.AnnualRevenue > 1000000` to
mean "emit `AnnualRevenue > 1000000`", not "compare two values". That
means overloading comparison operators on `Field[T]` to return a
`Filter`. A spike (`.scratch/spike_typing.py`) checked the design under
mypy before any generator was written.

**Decision:** `Field[T]` overloads `<`, `<=`, `>`, `>=`, `==`, `!=` to
return `Filter`. Model classes stay pure schema descriptors: plain
annotated class attributes, never instantiated, never holding row data.
Four hardening measures came out of the spike.

1. **`Filter.__bool__` raises `TypeError`.** This is the primary safety
   mechanism, not a formality. A filter is never legitimately evaluated
   for truthiness, so every path that does so — `if`, `and`/`or`, `in`,
   `sorted`, `assert` — is a mistake. Without this they pass silently
   and produce a wrong query; with it they fail loudly at the point of
   the mistake. The message names the two likeliest causes.

2. **`__ne__` is defined explicitly.** Python derives `__ne__` by
   negating `__eq__`'s result, which returned `False` in the spike
   instead of a `Filter` — a silent wrong answer that mypy did not flag,
   since `bool` is a valid `__ne__` return. With measure 1 in place the
   derived version would now raise instead, but users expect `!=` to
   work, so it builds a `!=` filter.

3. **`__hash__ = object.__hash__` — identity hashing.** Defining
   `__eq__` sets `__hash__` to `None`, which made `Field` unhashable and
   would break every set and dict the query builder needs (dedupe,
   projection maps).

   *This reversed during review.* The first version hashed the field
   name, which bought nothing and laid a trap: two `Field` objects
   sharing a name hash equal, so the set falls back to `__eq__`, which
   returns a `Filter`, whose `__bool__` raises via measure 1 — a
   `TypeError` from what looks like an ordinary `set()` call. Identity
   hashing has no such fallback, and dedupes correctly for every real
   use, because a generated model holds exactly one `Field` object per
   column. SQLAlchemy takes the same approach for the same reason. Where
   the query builder needs name-level dedupe it will do it explicitly,
   where the intent is visible.

4. **`# type: ignore[override]` on `__eq__` and `__ne__` only.** mypy
   correctly reports that returning `Filter` from a narrowed parameter
   violates `object.__eq__`'s `(object) -> bool` — both the return type
   and the argument type. The violation is the design, and it is the
   trade-off SQLAlchemy makes for column expressions. Silenced per
   operator rather than module-wide, so an unrelated override still
   errors. `<`, `<=`, `>`, `>=` need no ignore: `object` does not define
   them.

**Consequence:** What the spike confirmed under mypy: a mistyped
comparison (`Account.Name > 5`) and an unknown attribute
(`Account.Nonexistent`) are both static errors, and a valid comparison
infers as `Filter`. That is the product working as intended — the type
checker, not a runtime exception, is what catches a bad query.

**Also decided during review:** `render_literal` raises `ValueError` on
a naive `datetime`, with a message saying to attach a timezone. It had
been rendering one without an offset, which Salesforce rejects — so the
error surfaced at the API boundary rather than at the mistake. Refusing
to render output we know is invalid is the same crash-early principle as
the nameless-field guard in D6. This is not a timezone *policy*: what
offset a caller should use, and whether the builder should supply a
default, stays open and belongs with the query builder.

## D8 — SOQL escaping follows the documented sequence list (2026-08-09)

**Context:** The query builder interpolates caller-supplied values into
SOQL strings. Escaping is the whole risk surface of that stage. The
prior `render_literal` escaped backslash and single quote — 2 of the 8
sequences Salesforce actually documents.

**Decision:** Escape exactly the set in the SOQL and SOSL Reference,
"Quoted String Escape Sequences": `\\`, `\'`, `\"`, `\n`, `\r`, `\t`,
`\b`, `\f`. Control characters with no documented sequence are emitted
as `\uXXXX`, which the same page documents for arbitrary code points.
One implementation — `fields.escape_string`, used by `render_literal`
and by nothing else. A second escaper is how one gets fixed and the
other does not.

Escaping walks the string once, rewriting each character at most once,
so a backslash introduced by one rule can never be re-escaped by
another. Sequential `.replace()` calls are the classic way an escaper
gets defeated.

**`_` and `%` are NOT escaped by default.** The docs list `\_` and `\%`
as LIKE-only sequences. Outside a LIKE pattern those characters are
ordinary; inside one they are the wildcards the caller asked for, and
escaping them silently would break every intentional prefix search.
`escape_like_wildcards()` is provided for a LIKE pattern carrying user
data that must match literally. The caller chooses — this is the one
place the library cannot decide for them, because both readings are
legitimate.

**Consequence:** A payload cannot terminate the literal. The dangerous
case is a trailing backslash: undoubled, it would escape the literal's
own closing quote and let the remainder run as SOQL. Tested, along with
quotes, `' OR 1=1 --`, newlines, tabs, undocumented control characters,
and unicode. Non-ASCII passes through as itself — SOQL is UTF-8, and
escaping it would only make queries unreadable without making them
safer.

Note the docs label `\b` "Bell" while Python's `\b` is backspace
(0x08). We map the character to the sequence and let the org interpret
it; nothing downstream depends on which it is.

**Conditions combine with `&` and `|`, and each side is parenthesized in
the output.** Python binds `&` tighter than `>`, so callers must
parenthesize each operand — `(A == 1) & (B > 2)`. We document that
rather than try to defeat Python's precedence; it is SQLAlchemy's
constraint for the same reason. `and`/`or` evaluate truthiness and
therefore raise (D7), and the message names `&` and `|`, so the likeliest
mistake is a loud error that says what to do instead.

**`ORDER BY` supports both directions**: `order_by(A, desc=True)`
renders `ORDER BY A DESC`, ascending stays the default, and `desc`
applies to every field in that call. Mixed directions come from chaining
calls, which keeps the common cases short and the rare one possible
without a per-field wrapper type.

**Field ownership is checked by identity everywhere** — `select()`,
`order_by()` and `where()` alike.

*This changed during review.* The first version had `Filter` store the
field *name*, so `where()` could only check names. That was not an
inconsistency, it was a silent wrong-answer path:
`Contact.CreatedDate > x` used in an `Account` query type-checks (both
are `Field[datetime]`), passes a name check (Account has a
`CreatedDate` too), and renders as valid SOQL that filters the wrong
object's column. Nothing anywhere would report it. The fields with
names shared across sObjects — `Id`, `Name`, `CreatedDate`, `OwnerId` —
are the ones people filter on most, so this was the common case rather
than an exotic one.

`Filter` now holds the `Field` object and `Condition.fields()` returns
Field objects, which is what makes the identity check possible. Identity
hashing (D7) is what makes them usable in the sets that check does.