"""Stage 6: hand a rendered query to a caller-supplied client and drain it.

The last gap in the three parts. `query` ends at a SOQL string; without this
the caller carries that string to a client themselves, and the moment they do,
they own the paging — which is where the accident happens.

**Draining the cursor is the reason this module exists.** A Salesforce query
returns the first batch (2000 rows by default) plus a ``nextRecordsUrl`` when
more exist. Code that reads ``response["records"]`` and stops has 2000 rows and
no indication that is not the answer. That is a plausible result in place of an
error — the one failure this project is built to prevent — and it is the normal
outcome of doing this by hand, not an unusual one.

What this module does not do (D1, narrowed by D15): auth, HTTP, sessions,
retries, result mapping. The caller constructs and owns the client. We never
read a credential. We call two read-only methods on an object we were handed,
walk the dicts that come back, and yield rows exactly as the API returned them.

Nothing here writes. :class:`SalesforceClient` declares two methods, both
reads, and no code path touches anything else — asserted in the tests rather
than promised here.
"""

from collections.abc import Iterator
from importlib.util import find_spec
from typing import Any, Protocol, runtime_checkable

from soqlmodel.errors import ExecuteError
from soqlmodel.query import Query

# Rows are returned exactly as the API sends them: no mapping onto generated
# models in v1. The snapshot says what the fields are; this says what came back.
Row = dict[str, Any]

INSTALL_HINT = 'pip install "soqlmodel[salesforce]"'


@runtime_checkable
class SalesforceClient(Protocol):
    """The part of ``simple_salesforce.Salesforce`` this module uses.

    Structural, and deliberately so: declaring the shape instead of importing
    the class is what makes simple-salesforce an *optional* dependency rather
    than a hard one. Nothing in soqlmodel imports it, at runtime or for typing,
    so the package installs and runs snapshot/generate/check with it absent —
    and a real ``Salesforce`` satisfies this without knowing we exist.

    Two methods, both reads. A client is welcome to have more; we will not call
    them.
    """

    def query(self, query: str) -> Any: ...

    def query_more(self, next_records_identifier: str, identifier_is_url: bool = False) -> Any: ...


def _require_client(client: object) -> None:
    """Reject anything that cannot answer the two calls we are about to make.

    Checked up front rather than at the first attribute access, so the error
    names the problem instead of surfacing as ``AttributeError: 'str' object
    has no attribute 'query'`` from inside a generator three frames down.

    The message forks on whether simple-salesforce is importable, because the
    two causes want different fixes. Not installed is the common one and the
    fix is a pip command; installed but the wrong object was passed is a
    different mistake and a pip command would be a red herring.
    """
    if isinstance(client, SalesforceClient):
        return

    missing = [
        name for name in ("query", "query_more") if not callable(getattr(client, name, None))
    ]
    got = type(client).__name__

    if find_spec("simple_salesforce") is None:
        raise ExecuteError(
            f"execute needs a Salesforce client and got {got}, which has no "
            f"{' or '.join(missing)}; simple-salesforce is not installed. "
            f"Install it with: {INSTALL_HINT}"
        )

    raise ExecuteError(
        f"execute needs a Salesforce client and got {got}, which has no "
        f"{' or '.join(missing)}; pass a simple_salesforce.Salesforce instance"
    )


def _unpack(batch: object, soql: str) -> tuple[list[Row], bool, str | None]:
    """Validate one response batch into (records, done, next url).

    Every branch here is a response the API should never send. They raise
    anyway: the alternative to crashing on a malformed cursor is returning
    some-but-not-all rows, and a short answer that looks complete is worse
    than no answer at all.
    """
    if not isinstance(batch, dict):
        raise ExecuteError(f"the client returned {type(batch).__name__}, not a dict, for: {soql}")

    records = batch.get("records")
    if not isinstance(records, list):
        raise ExecuteError(
            f"the client's response has no 'records' list (got "
            f"{type(records).__name__}) for: {soql}"
        )

    done = batch.get("done")
    if not isinstance(done, bool):
        # Not defaulting to True. Guessing "done" on a response we do not
        # understand is exactly how a partial result gets returned as a whole
        # one.
        raise ExecuteError(
            f"the client's response has no boolean 'done' (got {done!r}) for: {soql}"
        )

    next_url = batch.get("nextRecordsUrl")
    if not done and not isinstance(next_url, str):
        raise ExecuteError(
            f"the client reported more rows to fetch but gave no "
            f"'nextRecordsUrl' (got {next_url!r}); the cursor broke mid-drain "
            f"and these rows are an incomplete answer to: {soql}"
        )

    return records, done, next_url


def execute_iter(query: Query, client: SalesforceClient) -> Iterator[Row]:
    """Run ``query`` and yield every matching row, fetching batches as needed.

    Lazy: the first batch is not fetched until the first row is pulled, and a
    later batch is not fetched until the previous one is exhausted. For a
    result set too large to hold in memory this is the honest tool — there is
    no row cap here, by design (D15), because ``Query.limit`` already caps
    server-side and a client-side cap would either fail a legitimate export or
    truncate one silently.

    The client's own exceptions are not caught. A bad SOQL string, an expired
    session, a connection reset — those are theirs, and they say it better.

    Raises:
        ExecuteError: if ``client`` cannot answer our calls, or if a response
            is malformed, or if the cursor stops making progress.
    """
    _require_client(client)
    return _drain(query.render(), client)


def _drain(soql: str, client: SalesforceClient) -> Iterator[Row]:
    """The cursor walk itself, split out so :func:`execute_iter` can validate
    the client eagerly — a generator body does not run until first iteration,
    and a bad client should fail at the call, not at the first row."""
    batch = client.query(soql)
    seen: set[str] = set()

    while True:
        records, done, next_url = _unpack(batch, soql)
        yield from records

        if done:
            return

        # next_url is a str here: _unpack raises otherwise when not done.
        assert next_url is not None
        if next_url in seen:
            # Salesforce advances the cursor on every call, so a repeat means
            # the client or a proxy is looping. Raising beats hanging: a
            # pipeline that never returns gives no error and no output, which
            # is harder to diagnose than a crash.
            raise ExecuteError(
                f"the client returned the same nextRecordsUrl twice "
                f"({next_url}); the cursor is not advancing, for: {soql}"
            )
        seen.add(next_url)

        batch = client.query_more(next_url, identifier_is_url=True)


def execute(query: Query, client: SalesforceClient) -> list[Row]:
    """Run ``query`` and return every matching row.

    Every row, not the first batch — see the module docstring. Use
    :func:`execute_iter` when the result set should not be held in memory.

    Raises:
        ExecuteError: as :func:`execute_iter`.
    """
    return list(execute_iter(query, client))
