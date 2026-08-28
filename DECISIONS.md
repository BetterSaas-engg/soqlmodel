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

## D9 — Snapshots are scoped to a declared dependency (2026-08-11)

**Context:** An unscoped Account snapshot holds 239 fields. A pipeline
reads four of them. `check` would report drift on the other ~200 —
fields nobody in the project has ever referenced. A detector that cries
wolf gets ignored, and an ignored detector gets deleted.

There is a second, larger reason. A snapshot of *everything* mirrors the
org; a *scoped* snapshot declares a dependency. Only the second makes the
project's purpose statement true, and only the second is a contract
between the team that administers the org and the team that depends on
it. `soqlmodel.toml` is that contract, in the repo, in review.

**Decision:** `soqlmodel.toml` at the project root declares scope:

    org = "FULL Sandbox"

    [objects]
    Account = ["Name", "AnnualRevenue", "Contract_End__c"]
    Opportunity = ["*"]

A list scopes to those fields; `"*"` means all of them; an object not
mentioned is unscoped; **no config file at all means everything**, so the
tool works before anyone has configured it and narrows only once someone
declares a dependency.

`build_snapshot(describe, org, fields=None)` takes the filter. A scoped
snapshot records `requested_fields`, so `check` can distinguish "a field
I asked for is gone" (breaks the pipeline) from "a field I never asked
for" (not my problem). Unscoped snapshots omit the key entirely, which
is why this needs no `format_version` bump under D6: every snapshot that
could already exist is byte-identical to what it was.

**A requested field the org does not have is an error — but only for
the stages that are declaring a dependency.** `build_snapshot` takes
`strict=True` by default: it names the field and the sObject, and
suggests the right casing when only case differs. Silently dropping it
would generate a model missing an attribute the pipeline references —
code that fails later, further from the cause. Same crash-early
principle as D6's nameless-field guard and D7's naive-datetime refusal.

`check` passes `strict=False` and reads `missing_fields(snapshot)`
instead. **The rule is per stage, not per project:** raising is right
when you are declaring what you need, and wrong when you are asking what
changed. A declared field disappearing is the single most important
drift case there is; if `check` inherited the raise, that case would
produce a stack trace instead of the CRITICAL line it deserves. A
snapshot built either way is byte-identical when nothing is missing, so
the flag changes error behaviour and nothing else.

**Consequence:** Measured against the real org: 239 fields → 4, and
112,715 bytes → 1,948. A diff of that snapshot is readable in full by a
person, which is the entire point. Determinism is unaffected —
`requested_fields` is sorted and deduped, and scoped snapshots are
byte-identical across runs.

**`requires-python` is `>=3.11`, and there are no runtime
dependencies.** Config parsing needs TOML; `tomllib` is stdlib from
3.11, and on 3.10 it would mean depending on `tomli`.

*This reversed during review.* The first version kept the 3.10 floor and
took the dependency. Two reasons it went the other way. Widening
`requires-python` later is not a breaking change while narrowing it is,
so 3.11 is the reversible choice in both directions — a 3.10 user can be
supported later by adding the backport, but a dependency cannot be
removed from under anyone. And zero runtime dependencies is worth more
to a library that data teams vendor into pipelines than two months of
3.10 support (3.10 reaches EOL in October 2026).

## D10 — CRITICAL fails the build, WARNING does not (2026-08-11)

**Context:** `check` classifies drift. The severity line is the product:
too strict and every picklist edit by an admin turns a deploy red, too
loose and a broken pipeline ships. Both failure modes end the same way —
the check gets muted, and then the CRITICALs stop being seen either.

**Decision:** Two severities, and only one of them fails.

CRITICAL — a pipeline is broken, or is about to return wrong numbers:
a declared field no longer exists; a field's type changed; a picklist
value was removed (a mapping keyed on it is now dead, per D3/D4);
`filterable` or `sortable` became false, breaking existing WHERE and
ORDER BY clauses; `deprecatedAndHidden` became true.

WARNING — real information, breaks nothing that already runs: a picklist
value was added (silently unmapped, which is precisely why D3 stores
values at all); `nillable` changed; `length`, `precision` or `scale`
changed; a new field appeared on an object scoped with `"*"`.

`exit_code` is 1 if any CRITICAL is present, else 0. **WARNINGs alone do
not fail the build.** A value being added to a picklist is worth reading
on a Monday morning; it is not a reason to block a deploy at 5pm, and a
check that blocks on things nobody can act on immediately is a check
people learn to skip. The asymmetry is the point: CRITICAL has to be
rare enough that a red build means something.

Gaining a capability is never reported — `filterable` going false→true
breaks nothing, so it produces no line at all. Silence on harmless
change is what keeps the report readable.

**A `format_version` mismatch short-circuits the whole diff** and
returns one CRITICAL telling the user to regenerate. Per D6 that
difference is our format moving, not the org's schema; diffing across
formats would report our own reformatting as drift, which is the exact
false positive this project exists to prevent. It is CRITICAL because
the check could not actually run — reporting "no drift" from a diff that
never happened would be worse than failing.

**Consequence:** `diff_snapshots` is pure — two dicts in, changes out —
so every rule above is tested against fixtures rather than an org. The
live side is built with `strict=False` (D9), which is load-bearing: a
declared field that has vanished is the most important drift case there
is, and under `strict=True` it would arrive as a traceback instead of
the CRITICAL line it deserves.

## D11 — Two error hierarchies: user failure vs. bug (2026-08-26)

**Context:** The CLI turns an exception into `soqlmodel: <message>` and
exit 2. The first version caught `(ValueError, OSError, SfCliError)`.
`ValueError` is raised throughout the standard library and throughout
this codebase, so a genuine bug — a stray `ValueError` from the query
builder, a `JSONDecodeError` from a half-written file — would be dressed
up as a tidy one-line user error with the traceback suppressed. That is
a plausible outcome in place of an error, which is the single failure
mode this project exists to prevent. A drift detector that reports a
defect as "bad config" is worse than one that crashes.

**Decision:** Two kinds of failure, and the type says which.

A **user failure** is a wrong config, an unreachable org, a declared
field the org does not have. Every one of them raises a subclass of
`SoqlModelError`, defined in `errors.py`, one class per stage:
`ConfigError`, `SfCliError`, `SnapshotError`, `GenerateError`, plus
`MissingSnapshotError`. The CLI catches `(SoqlModelError, OSError)` and
nothing else.

A **programming failure** is a caller misusing the library: ordering by
a `Field` from another sObject, comparing against a naive datetime,
`limit(0)`. Those keep raising plain `ValueError` and `TypeError`. The
CLI does not catch them, so they reach the user as a traceback — the
correct output for a bug.

**Nothing in `errors.py` subclasses `ValueError`.** The whole value of
the split is that `except SoqlModelError` cannot swallow a defect, and a
`ValueError` base would hand that property straight back. It is asserted
in `tests/test_errors.py` rather than left to convention.

`MissingSnapshotError` inherits both `SoqlModelError` and
`FileNotFoundError`. It is a user failure *and* it is literally a
missing file; code that already catches the stdlib class keeps working.

**Consequence:** `query.py` and `fields.py` are the only modules that
still raise bare `ValueError`/`TypeError`, and that is the tell for
"this is the caller's bug, not the user's mistake". A new raise site has
to answer which kind it is before it can pick a class. The reverse cost
is real: `json.JSONDecodeError` is a `ValueError`, so a corrupt snapshot
stopped being caught for free and had to be wrapped deliberately in
`read_snapshot` — which is the point. Failures are now caught because
someone decided they should be, not because they happened to share a
base class with everything else.

## D12 — Global options resolve to the later occurrence (2026-08-26)

**Context:** `--config`, `--schema-dir` and `--org` are accepted on both
sides of the subcommand, because both `soqlmodel --schema-dir x check`
and `soqlmodel check --schema-dir x` are things people type. That makes
`soqlmodel --schema-dir a snapshot --schema-dir b` legal and ambiguous.
Argparse resolves it one way by accident; leaving it there means the
answer is whatever the implementation happens to do this release.

**Decision:** **The occurrence after COMMAND wins.** Stated in
`--help`, and pinned by test at both the parser level and through
`main`.

It is the same rule argparse already applies to an option repeated in
one position — last wins — so there is one rule rather than a special
case for the subcommand boundary. `default=argparse.SUPPRESS` on the
shared options is what makes the other direction work: an option the
user did not type leaves no attribute behind, so the subparser's copy
cannot overwrite a value given before COMMAND with a default.

**Consequence:** SUPPRESS is load-bearing, not a stylistic choice —
removing it silently makes an option given before the subcommand
disappear. A mutation dropping it fails 25 tests.

## D13 — A snapshot with a BOM is refused; the config tolerates one (2026-08-26)

**Context:** `soqlmodel.toml` reads as `utf-8-sig` because PowerShell's
`Set-Content` and Notepad both write a UTF-8 BOM and `tomllib` rejects
it with an error naming line 1 column 1 and nothing else. The same
tolerance was extended to snapshot files. That was wrong, and in a
specific way: a BOM'd snapshot parsed clean, `check` reported "No
drift", and the bytes on disk were no longer the bytes `snapshot`
writes. The next `snapshot` run would silently drop the BOM and produce
a diff with no schema change behind it — phantom drift, from the tool
whose job is to not produce it.

**Decision:** The two files have different provenance, so they get
different policies.

`soqlmodel.toml` is **hand-written**, so it keeps tolerating a BOM. A
user editing a config in Notepad should not have to know what a byte
order mark is.

A snapshot is **only ever written by us**, so a BOM means the bytes on
disk are not the bytes we wrote. `read_snapshot` refuses it and names
the fix: re-run `snapshot`. Exit 2, not exit 1 — the check could not be
trusted, so it did not run, the same reasoning that makes a
`format_version` mismatch fail loudly under D10 rather than report
"clean" from a diff that never happened.

`read_snapshot` is the only reader, and it lives beside `write_snapshot`
so the rules about what a snapshot file may contain sit next to the
rules about how one gets written. `check` and `generate` both go through
it. It also wraps `JSONDecodeError` and `UnicodeDecodeError` into
`SnapshotError` naming the file, which under D11 they now need.

**Consequence:** "check is clean" means the committed file is what
`snapshot` produces, not merely something that parses to the same dict.
A U+FEFF *inside* a value is still data and is left alone — the guard is
about the first three bytes, not about the character. This does not yet
close the general case: a snapshot reindented by hand still parses to
the same dict and still reports clean. Canonical-form checking is a
larger idea and is not in v1.

## D14 — `ruff format` is enforced (2026-08-27)

**Context:** `ruff check` has been clean since D1. `ruff format` never
has been — 11 files on HEAD would be reformatted. Nothing said whether
that was a decision or an oversight, and SFM-11 is about to wire up CI.
CI would have silently ratified the oversight: a pipeline that runs only
`ruff check` is a pipeline that says formatting does not matter, forever,
because nobody revisits a green build.

**Decision:** Adopt it. `ruff format --check` runs in CI beside
`ruff check`, at the existing `line-length = 100`.

Three reasons, in increasing order of weight.

It costs nothing here. The reformat is pure line-joining — wrapped calls
and implicitly-concatenated strings that already fit inside 100
characters. No string *content* changes, so no error message a test
matches on moves. Nothing hand-tuned is lost, which is the usual reason
to refuse a formatter.

The moment is free. Adopting a formatter is cheap before CI exists and
annoying afterwards, when it means one reformat commit crossing every
open branch.

And the real reason: **`generate` emits Python source that users
commit.** If our own formatting is unenforced, we have no standard to
hold generated output to either — and generated output is exactly where
formatting stops being cosmetic. A user whose repo runs `ruff format`
will have it rewrap our output, and the next `soqlmodel generate` will
write it flat again. That is a diff on every run with no schema change
behind it, in the user's repo, from the tool whose entire purpose is to
stop phantom diffs. Same failure as the BOM in D13, one layer out.

**Consequence — and an open defect.** The rule this decision creates is
that generated output must already be `ruff format`-clean, so
regenerating never fights a user's formatter. **It is not, today.** A
field name over roughly 30 characters pushes its line past 100:

    Commission_Attainment_Rolling_12M__c: Field[str] = Field("Commission_Attainment_Rolling_12M__c", "picklist")

That is 110 characters, from a real object in a real org, and long API
names are the norm in Salesforce rather than the exception. It passes
`ruff check` only because `line-length` alone does not enable E501.

This decision does not fix that — the generator has to learn to wrap,
and that is its own ticket. What the decision does is make the defect
*expressible*: with no formatting standard there was no sense in which
the output was wrong, and it would have been found by a user instead of
by us. Recorded in KNOWN_ISSUES.md.

The reformat lands as its own mechanical commit, separate from both this
decision and from SFM-9, so that `git blame` crosses one commit that
changed no behaviour rather than being scattered through a review.

## D15 — execute owns the drain loop; D1's "pagination" means transport (2026-08-27)

**Context:** SFM-10 closes the last gap: `query` ended at a rendered
string and the caller carried it to a client themselves. The moment they
do that, they own the paging — and a Salesforce query returns the first
2000 rows plus a `nextRecordsUrl`, so code that reads
`response["records"]` and stops has a plausible answer instead of an
error. That is the failure this project exists to prevent, and by hand
it is the *normal* outcome rather than an unusual one.

**This narrows D1**, which said we do not implement "auth, HTTP,
pagination, or retries". Taken literally it forbids this ticket, and
simple-salesforce already ships `query_all` / `query_all_iter` that
drain for us.

**Decision:** Four parts.

**We own the drain loop; D1's "pagination" is read as transport.**
simple-salesforce still performs every request, session refresh and
retry — we never issue HTTP. What we own is ~10 lines walking already
decoded dicts, with no I/O of our own. Delegating to `query_all_iter`
would honour D1's letter, but there would then be no drain of ours to
test, and the paging guarantee — the reason the module exists — would
rest entirely on an untested dependency. It also buys a real
improvement: their loop does `result['nextRecordsUrl']` unguarded, so a
`done: false` response missing the URL raises a bare `KeyError`. Ours
raises `ExecuteError` saying the cursor broke mid-drain and these rows
are an incomplete answer.

**The client is a `runtime_checkable` Protocol, not a duck-typed
`Any`.** Structural typing is what keeps simple-salesforce *optional*:
we declare the two methods we call rather than importing the class, so
nothing in soqlmodel imports it at runtime or for typing, and
snapshot/generate/check all work with it absent. A duck-typed client
would be `Any`, which mypy cannot check at all — unacceptable in a
project whose central claim is static checkability. `runtime_checkable`
additionally lets a wrong object be rejected at the call with a message
naming the missing method, instead of an `AttributeError` from inside a
generator.

**No row cap. Drain unbounded, and ship `execute_iter` alongside
`execute`.** `Query.limit` already caps *server-side*, which strictly
beats any client-side cap — it stops rows crossing the wire rather than
discarding them after paying for them. A cap that raised would make a
legitimate large export impossible; a cap that truncated would be the
exact plausible-wrong-answer failure this module is written against.
Memory exhaustion is a crash, not a wrong number, and this project
prefers the crash. `execute_iter` is the honest tool for a result set
that should not be held in memory, and it costs nothing: the drain is a
generator either way, and `execute` is `list(execute_iter(...))`.

**`execute` is a free function in its own module, not `Query.execute`.**
A method reads better as a chain, but CLAUDE.md's architecture says
stages 2-4 are pure with no network, and `query.py` says so in its own
docstring. Hanging a network call off `Query` would make that false.
`execute.py` imports `query`; `query` imports nothing new.

**Rows come back as `list[dict]`, exactly as the API returned them.** No
mapping onto generated models in v1 — that is SFM-10d.

**Consequence:** the client's own exceptions propagate untouched. A bad
SOQL string, an expired session, a connection reset — those are
simple-salesforce's to report, they word them better than we would, and
wrapping them into `ExecuteError` would destroy the type a caller wants
to catch on. `ExecuteError` is raised only for our own failures: a
client that cannot answer our calls, a malformed batch, a cursor that
stops advancing. That split is D11 applied one module further out, and
it is tested by a mutation that wraps client errors and must fail.

## D16 — "snapshot" is kept, despite the Salesforce collision (2026-08-27)

**Context:** Salesforce already uses "snapshot" for Reporting Snapshots
(formerly Analytic Snapshots), which periodically write *report results*
into a custom object. Our snapshot is a committed JSON file describing
*schema*. Same word, different thing, and the audience is people who
work in Salesforce daily. Parked since SFM-3; SFM-11 has to settle it
because the README is where a stranger meets the term.

**Decision:** Keep `snapshot`. Say **schema snapshot** where ambiguity
is possible, and address the collision explicitly in the README rather
than hoping nobody notices.

The alternatives are worse. "Lock" (`soqlmodel.lock`) implies dependency
resolution — a lockfile pins a solved version set, and nothing here is
solved or resolved. "Freeze" implies pinning something that would
otherwise drift on its own, but the org drifts whether or not we
record it. "Schema dump" describes a mirror, which is precisely what
D9 decided this is *not* — it is a scoped declaration of dependency.
"Manifest" is closest in spirit but is taken twice over in this space,
by `package.xml` manifests in SFDX and by the term's general use for
deploy payloads.

Against that, "snapshot" is broadly understood outside Salesforce — VM
snapshots, database snapshots, snapshot testing — and all of those carry
the right connotation: a point-in-time record you compare against later.
The collision is with a niche reporting feature that a data engineer
integrating a pipeline is unlikely to have in mind, and the surrounding
context disambiguates immediately: the files live in `schema/`, the
config is `soqlmodel.toml`, and the command sits beside `generate` and
`check`.

**Consequence:** a rename would have been cheap now and expensive later
— the CLI verb, the directory, `snapshot_all`, `MissingSnapshotError`,
`SNAPSHOT_FORMAT_VERSION` and the docs all carry it. Deciding to keep it
is therefore a real decision with a real cost if wrong, which is why it
is written down rather than left implicit. If user confusion shows up in
practice, the migration is a CLI alias plus a directory default, not a
rewrite.

## D17 — the generated-output wrap defect blocks the v1 release (2026-08-27)

**Context:** SFM-11 had to decide whether SFM-10c — `generate` emitting
lines past `line-length = 100` — blocks publishing to PyPI or ships as a
documented limitation.

**Measured before deciding**, against three real objects (Account,
Contact, Opportunity, all unscoped) from a live org:

- 592 generated field lines, **18 over 100 characters** (3.0%), longest
  132.
- Overflow begins at an API name length of **32 characters**.

3% sounds ignorable. It is not, because the unit that matters is not the
line, it is the **file**: one overflowing line makes the whole module
non-`ruff format`-clean. All three objects tested produce a dirty
module. In a repository that runs a formatter, this fires on essentially
every real generate, not on 3% of them.

**Decision:** It blocks the release. Fix SFM-10c first.

The reasoning is not severity, it is *subject matter*. This project's
pitch is that a schema change surfaces as a failing build instead of
wrong numbers, and the supporting property — the one D5, D13 and D14 all
exist to protect — is that regenerating produces no diff unless the org
moved. A generator whose output a formatter rewrites, and which then
rewrites back on the next run, produces a phantom diff on every run.
That is the exact failure this tool claims to eliminate, shipped inside
the tool, on the first artifact a new user sees.

Shipping it with a README note would be defensible for most projects.
Not for this one: "regenerate produces a spurious diff" is the single
worst first impression this particular package could make, and it would
undercut the pitch in the same session a user first tries it.

The fix is small — the emitter is one f-string — which makes shipping
around it harder to justify, not easier.

**Consequence:** v0.1.0 is not published. The release sequence is
SFM-10c, then a license, then TestPyPI, then PyPI.

**A second, independent blocker surfaced while writing the README: there
is no license.** No `LICENSE` file and no `license` key in
`pyproject.toml`, which means default copyright — all rights reserved —
and nobody can legally use the package. That is not a default to apply
quietly on the owner's behalf, so it is recorded rather than chosen. The
README says so plainly instead of implying a permissive license that has
not been granted.

## D18 — generated output is stable under a formatter, via the magic trailing comma (2026-08-27)

**Context:** D17 blocked the release on `generate` emitting lines past
the line length. The obvious reading — "make the lines short" — is
wrong, and the ticket said so: the requirement is that generated output
**survives `ruff format` unchanged**, because `models.py` gets committed
by the user and reformatted by their toolchain.

Short lines are not sufficient, because a formatter also *joins*. A
call wrapped across lines that would fit on one gets collapsed. So
"always wrap" fails exactly as badly as "never wrap", in the opposite
direction, and both produce the same phantom diff on every `generate`.

**Decision:** Two forms, chosen per field against a configurable
`line_length`.

Under the limit, one line. Over it, wrapped with a **magic trailing
comma**:

    Commission_Attainment_Rolling_12M__c: Field[str] = Field(
        "Commission_Attainment_Rolling_12M__c",
        "picklist",
    )

The trailing comma is the mechanism, not styling. Ruff and Black both
read it as "the author wants this exploded" and leave the call alone at
*any* line length. Measured at 79, 88, 100, 120 and 200: the wrapped
form never moves; the same call without the comma is collapsed at every
one of them. Both directions are asserted in
`tests/test_generated_format_stability.py` so the claim rests on the
formatter's observed behaviour rather than on ours.

**`line_length` is configurable, defaulting to 88.** This is the part
that makes the guarantee real rather than nearly-real. The wrapped form
is stable everywhere, but the one-line form is only stable for a
formatter configured at or above the value we generated for. A fixed
constant would therefore leave some users churning no matter which
constant was picked — measured across three real objects (592 fields),
a threshold of 88 leaves 106 lines that a formatter set to 79 would
rewrite. Matching the user's setting removes the gap entirely. 88 is
ruff's and Black's default, so most projects set nothing.

**Consequence:** verbosity scales with how narrow the formatter is. On
the same 592 real fields: 621 lines at 120, 663 at 100, 843 at 88, 1161
at 79. That is the price of stability and it is worth paying — a
generated file is read rarely and regenerated constantly.

The wrapped form deliberately puts each argument on its own line rather
than packing them, because packed arguments plus a trailing comma is not
a form ruff produces, and generating something the formatter would
rewrite is the entire bug.

Determinism (D5) is unaffected and still tested: same snapshot and same
`line_length` produce byte-identical text.