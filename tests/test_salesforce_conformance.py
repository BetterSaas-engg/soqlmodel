"""Conformance against the real ``simple_salesforce``, without an org.

SFM-10 left one gap: no real ``Salesforce`` object had ever answered our two
calls. The offline Protocol test in test_execute.py transcribes its signatures,
and a transcription can rot — rename a parameter upstream and that test keeps
passing while real usage breaks.

`isinstance`/`issubclass` do not close that gap. A runtime_checkable Protocol
checks *method names only*: a class declaring ``query(self, soql_text)`` and
``query_more(self, locator, as_url=False)`` passes ``issubclass`` and would
break every call we make. Verified, not assumed — see
``test_issubclass_alone_does_not_catch_renamed_parameters`` below.

What does close it: stub the transport and let the genuine
``Salesforce.query`` / ``Salesforce.query_more`` run. Both build their URL and
delegate to ``_call_salesforce``, so replacing that one method exercises
everything above it — parameter names, the ``identifier_is_url`` branch, the
response parsing — against our real drain. No network, no credentials, no org.

Skipped when the extra is absent, which is the normal local state. CI sets
SOQLMODEL_REQUIRE_SALESFORCE_EXTRA=1, which turns the skip into a failure so
the job cannot pass by quietly running nothing.
"""

import os

import pytest

_REQUIRED = os.environ.get("SOQLMODEL_REQUIRE_SALESFORCE_EXTRA") == "1"

try:
    from simple_salesforce import Salesforce
except ImportError:  # pragma: no cover - exercised by the CI job, not locally
    if _REQUIRED:
        raise
    pytest.skip(
        "simple-salesforce is not installed; it is an optional extra",
        allow_module_level=True,
    )

from soqlmodel.execute import SalesforceClient, execute, execute_iter
from soqlmodel.fields import Field
from soqlmodel.query import select


class Account:
    Id: Field[str] = Field("Id", "id")
    Name: Field[str] = Field("Name", "string")


QUERY = select(Account, Account.Id, Account.Name)


class FakeResponse:
    """Stands in for the ``requests`` response ``_call_salesforce`` returns."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self, **kwargs: object) -> dict:
        return self._payload


def client_with(*batches: dict) -> tuple[Salesforce, list[dict]]:
    """A real Salesforce whose transport is stubbed, plus a call log.

    Constructed with a session id and instance url, which does no I/O — the
    stub replaces the only method that would.
    """
    sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="stub")
    remaining = list(batches)
    calls: list[dict] = []

    def fake_call(method, url, name=None, **kwargs):
        calls.append({"method": method, "url": url, "name": name, "kwargs": kwargs})
        return FakeResponse(remaining.pop(0))

    sf._call_salesforce = fake_call  # type: ignore[method-assign]
    return sf, calls


def batch(*ids: str, done: bool = True, url: str | None = None) -> dict:
    body: dict = {
        "records": [{"Id": i, "Name": f"Account {i}"} for i in ids],
        "done": done,
        "totalSize": len(ids),
    }
    if url is not None:
        body["nextRecordsUrl"] = url
    return body


# --- the real class, driven by our drain -------------------------------------


def test_a_real_salesforce_drains_every_batch():
    """The claim SFM-10 could not make: simple_salesforce's own query and
    query_more, running our paging, returning every row."""
    sf, calls = client_with(
        batch("a", "b", done=False, url="/services/data/v59.0/query/01g-1"),
        batch("c", "d", done=False, url="/services/data/v59.0/query/01g-2"),
        batch("e", done=True),
    )

    rows = execute(QUERY, sf)

    assert [row["Id"] for row in rows] == ["a", "b", "c", "d", "e"]
    assert [call["name"] for call in calls] == ["query", "query_more", "query_more"]


def test_the_soql_reaches_the_real_client_intact():
    sf, calls = client_with(batch("a", done=True))

    execute(QUERY, sf)

    assert calls[0]["kwargs"]["params"] == {"q": "SELECT Id, Name FROM Account"}
    assert calls[0]["url"].endswith("/query/")


def test_query_more_is_called_with_identifier_is_url_and_builds_that_url():
    """The specific call shape a renamed parameter would break. Our drain
    passes ``identifier_is_url=True``; if that keyword ever disappears this
    raises TypeError instead of silently querying the wrong endpoint."""
    sf, calls = client_with(
        batch("a", done=False, url="/services/data/v59.0/query/01gABC-2000"),
        batch("b", done=True),
    )

    execute(QUERY, sf)

    # identifier_is_url=True means the full URI is used, not appended to base_url.
    assert calls[1]["url"] == (
        "https://example.my.salesforce.com/services/data/v59.0/query/01gABC-2000"
    )


def test_a_real_salesforce_satisfies_the_protocol():
    sf, _ = client_with(batch(done=True))

    assert isinstance(sf, SalesforceClient)


def test_execute_iter_is_lazy_against_the_real_client():
    sf, calls = client_with(
        batch("a", "b", done=False, url="/q/1"),
        batch("c", done=True),
    )

    stream = execute_iter(QUERY, sf)
    assert calls == []

    next(stream)
    assert len(calls) == 1


def test_a_real_client_error_propagates_unchanged():
    """simple-salesforce's own exception type, not wrapped by us."""
    from simple_salesforce.exceptions import SalesforceMalformedRequest

    sf = Salesforce(instance_url="https://example.my.salesforce.com", session_id="stub")

    def boom(*args, **kwargs):
        raise SalesforceMalformedRequest("url", 400, "query", "INVALID_FIELD")

    sf._call_salesforce = boom  # type: ignore[method-assign]

    with pytest.raises(SalesforceMalformedRequest):
        execute(QUERY, sf)


# --- why isinstance alone is not enough --------------------------------------


def test_issubclass_alone_does_not_catch_renamed_parameters():
    """Justifies everything above. If this ever fails, the Protocol got
    stricter and the transport-stub tests could be reconsidered."""

    class Renamed:
        def query(self, soql_text): ...

        def query_more(self, locator, as_url=False): ...

    assert issubclass(Renamed, SalesforceClient)


def test_the_real_signatures_still_accept_our_call_shape():
    """Belt and braces at the signature level, so a break names the parameter
    rather than surfacing as a TypeError deep in a drain."""
    import inspect

    query = inspect.signature(Salesforce.query)
    query_more = inspect.signature(Salesforce.query_more)

    assert "query" in query.parameters
    assert "next_records_identifier" in query_more.parameters
    assert "identifier_is_url" in query_more.parameters
