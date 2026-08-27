"""Tests for handing a query to a client and draining the cursor.

No live org, no network, no simple-salesforce. Every client here is a fake
built in this file, which is the point of the structural Protocol: the seam is
testable without the dependency that normally sits behind it.
"""

import sys

import pytest

from soqlmodel.errors import ExecuteError, SoqlModelError
from soqlmodel.execute import SalesforceClient, execute, execute_iter
from soqlmodel.fields import Field
from soqlmodel.query import select


class Account:
    """Stands in for a generated model."""

    Id: Field[str] = Field("Id", "id")
    Name: Field[str] = Field("Name", "string")


QUERY = select(Account, Account.Id, Account.Name)
SOQL = "SELECT Id, Name FROM Account"


def rows(*ids: str) -> list[dict]:
    return [{"Id": i, "Name": f"Account {i}"} for i in ids]


class FakeClient:
    """A client returning a fixed list of batches, recording what was called.

    Deliberately not a Mock: the recording is the assertion surface for "we
    never call anything that writes", and a Mock answers every attribute.
    """

    def __init__(self, *batches: dict) -> None:
        self._batches = list(batches)
        self.calls: list[tuple[str, tuple, dict]] = []

    def _next(self, name: str, args: tuple, kwargs: dict) -> dict:
        self.calls.append((name, args, kwargs))
        if not self._batches:
            raise AssertionError(f"{name} called more times than there are batches")
        return self._batches.pop(0)

    def query(self, query: str, **kwargs: object) -> dict:
        return self._next("query", (query,), kwargs)

    def query_more(self, next_records_identifier: str, **kwargs: object) -> dict:
        return self._next("query_more", (next_records_identifier,), kwargs)

    @property
    def method_names(self) -> list[str]:
        return [name for name, _, _ in self.calls]


def batch(*ids: str, done: bool = True, url: str | None = None) -> dict:
    body: dict = {"records": rows(*ids), "done": done, "totalSize": len(ids)}
    if url is not None:
        body["nextRecordsUrl"] = url
    return body


# --- paging: the reason this module exists ----------------------------------


def test_drains_every_batch_not_just_the_first():
    """AC1/AC2. Three batches, and all three must come back.

    This is the test the whole ticket turns on: a user who gets batch one and
    assumes it is the answer has a plausible result instead of an error.
    """
    client = FakeClient(
        batch("a", "b", done=False, url="/services/data/v64.0/query/01g-1"),
        batch("c", "d", done=False, url="/services/data/v64.0/query/01g-2"),
        batch("e", done=True),
    )

    result = execute(QUERY, client)

    assert [row["Id"] for row in result] == ["a", "b", "c", "d", "e"]
    assert len(result) == 5


def test_rows_keep_their_order_across_batches():
    """AC2 explicitly asks for order, not just count: a drain that gathered
    batches into a set or dict would pass a count check and lose the ordering
    an ORDER BY was for."""
    client = FakeClient(
        batch("1", "2", "3", done=False, url="/q/1"),
        batch("4", "5", "6", done=False, url="/q/2"),
        batch("7", "8", "9", done=True),
    )

    assert [row["Id"] for row in execute(QUERY, client)] == list("123456789")


def test_follows_the_next_records_url_it_was_given():
    client = FakeClient(
        batch("a", done=False, url="/services/data/v64.0/query/01gABC-2000"),
        batch("b", done=True),
    )

    execute(QUERY, client)

    assert client.calls[0] == ("query", (SOQL,), {})
    assert client.calls[1] == (
        "query_more",
        ("/services/data/v64.0/query/01gABC-2000",),
        {"identifier_is_url": True},
    )


def test_a_single_done_batch_makes_no_second_call():
    client = FakeClient(batch("a", "b", done=True))

    assert len(execute(QUERY, client)) == 2
    assert client.method_names == ["query"]


def test_an_empty_result_is_an_empty_list():
    client = FakeClient(batch(done=True))

    assert execute(QUERY, client) == []


def test_a_full_batch_that_is_done_is_not_followed():
    """done=True with a nextRecordsUrl still present. done wins — trusting the
    url instead would fetch past the end."""
    client = FakeClient(batch("a", done=True, url="/q/1"))

    assert len(execute(QUERY, client)) == 1
    assert client.method_names == ["query"]


def test_drains_many_batches():
    """Twenty batches, to catch a drain that handles two and stops."""
    batches = [batch(str(i), done=False, url=f"/q/{i}") for i in range(19)]
    batches.append(batch("19", done=True))
    client = FakeClient(*batches)

    assert [row["Id"] for row in execute(QUERY, client)] == [str(i) for i in range(20)]


# --- laziness ---------------------------------------------------------------


def test_execute_iter_does_not_fetch_a_batch_until_it_is_needed():
    client = FakeClient(
        batch("a", "b", done=False, url="/q/1"),
        batch("c", done=True),
    )

    stream = execute_iter(QUERY, client)
    assert client.method_names == []  # nothing fetched yet

    assert next(stream)["Id"] == "a"
    assert client.method_names == ["query"]  # first batch only

    assert next(stream)["Id"] == "b"
    assert client.method_names == ["query"]  # still batch one

    assert next(stream)["Id"] == "c"
    assert client.method_names == ["query", "query_more"]


def test_execute_iter_validates_the_client_before_iteration_starts():
    """A generator body does not run until first next(), so a bad client would
    otherwise fail somewhere unrelated to the call that caused it."""
    with pytest.raises(ExecuteError, match="needs a Salesforce client"):
        execute_iter(QUERY, "not a client")


# --- broken cursors ---------------------------------------------------------


def test_not_done_with_no_next_url_raises():
    """The malformed-cursor case. Returning these rows would be a short answer
    that looks complete."""
    client = FakeClient({"records": rows("a"), "done": False})

    with pytest.raises(ExecuteError, match="cursor broke mid-drain"):
        execute(QUERY, client)


def test_a_repeated_next_url_raises_rather_than_hanging():
    client = FakeClient(
        batch("a", done=False, url="/q/same"),
        batch("b", done=False, url="/q/same"),
    )

    with pytest.raises(ExecuteError, match="not advancing"):
        execute(QUERY, client)


def test_a_response_with_no_records_list_raises():
    client = FakeClient({"done": True, "totalSize": 0})

    with pytest.raises(ExecuteError, match="no 'records' list"):
        execute(QUERY, client)


def test_a_response_with_no_done_flag_raises():
    """Not defaulted to True: guessing 'done' is how a partial result gets
    returned as a whole one."""
    client = FakeClient({"records": rows("a")})

    with pytest.raises(ExecuteError, match="no boolean 'done'"):
        execute(QUERY, client)


def test_a_non_boolean_done_raises():
    client = FakeClient({"records": rows("a"), "done": "false"})

    with pytest.raises(ExecuteError, match="no boolean 'done'"):
        execute(QUERY, client)


def test_a_non_dict_response_raises():
    client = FakeClient()
    client._batches = [["not", "a", "dict"]]

    with pytest.raises(ExecuteError, match="not a dict"):
        execute(QUERY, client)


def test_a_malformed_second_batch_raises_after_the_first_succeeded():
    """The drain must keep validating, not just check the opening response."""
    client = FakeClient(
        batch("a", done=False, url="/q/1"),
        {"records": rows("b"), "done": False},
    )

    with pytest.raises(ExecuteError, match="cursor broke mid-drain"):
        execute(QUERY, client)


# --- the client's errors are the client's ------------------------------------


class Exploding:
    """A client whose own call fails, as simple-salesforce's would."""

    class ClientSideError(Exception):
        """Stands in for SalesforceMalformedRequest."""

    def __init__(self, on: str) -> None:
        self._on = on

    def query(self, query: str, **kwargs: object) -> dict:
        if self._on == "query":
            raise self.ClientSideError("INVALID_FIELD: No such column 'Nope__c'")
        return batch("a", done=False, url="/q/1")

    def query_more(self, next_records_identifier: str, **kwargs: object) -> dict:
        raise self.ClientSideError("INVALID_QUERY_LOCATOR")


def test_a_client_error_on_the_first_call_propagates_unchanged():
    """AC5. We do not wrap it: it is their error, they word it better, and
    wrapping would lose the type a caller wants to catch on."""
    with pytest.raises(Exploding.ClientSideError, match="No such column"):
        execute(QUERY, Exploding(on="query"))


def test_a_client_error_mid_drain_propagates_unchanged():
    with pytest.raises(Exploding.ClientSideError, match="INVALID_QUERY_LOCATOR"):
        execute(QUERY, Exploding(on="query_more"))


def test_a_client_error_is_not_converted_into_a_soqlmodel_error():
    """The guard on the guard: catching SoqlModelError must not catch this."""
    with pytest.raises(Exploding.ClientSideError):
        try:
            execute(QUERY, Exploding(on="query"))
        except SoqlModelError as exc:  # pragma: no cover - must not happen
            pytest.fail(f"client error was wrapped into {type(exc).__name__}")


# --- the client contract -----------------------------------------------------


def test_a_real_salesforce_shaped_object_satisfies_the_protocol():
    """simple_salesforce.Salesforce's actual signatures, transcribed. If the
    Protocol drifted from them, a real client would stop being accepted."""

    class SalesforceShaped:
        def query(self, query: str, include_deleted: bool = False, **kwargs: object) -> object:
            return {}

        def query_more(
            self,
            next_records_identifier: str,
            identifier_is_url: bool = False,
            include_deleted: bool = False,
            **kwargs: object,
        ) -> object:
            return {}

    assert isinstance(SalesforceShaped(), SalesforceClient)


@pytest.mark.parametrize("client", [None, "sf", 42, object()])
def test_an_object_that_is_not_a_client_is_refused(client):
    with pytest.raises(ExecuteError, match="needs a Salesforce client"):
        execute(QUERY, client)


def test_a_half_client_is_refused_and_the_message_names_what_is_missing():
    class QueryOnly:
        def query(self, query: str) -> dict:
            return batch(done=True)

    with pytest.raises(ExecuteError, match="query_more"):
        execute(QUERY, QueryOnly())


def test_the_error_names_the_dependency_and_how_to_install_it():
    """AC4. simple-salesforce is genuinely not installed in this environment,
    so this is the real message a user would see, not a simulated one."""
    if "simple_salesforce" in sys.modules:  # pragma: no cover
        pytest.skip("only meaningful with simple-salesforce absent")

    with pytest.raises(ExecuteError) as exc:
        execute(QUERY, None)

    assert "simple-salesforce is not installed" in str(exc.value)
    assert 'pip install "soqlmodel[salesforce]"' in str(exc.value)


# --- no writes, ever ---------------------------------------------------------


def test_the_client_contract_declares_only_reads():
    """AC6, at the contract. Anything that mutates would have to be added here
    first, so this test is the gate."""
    declared = set(SalesforceClient.__protocol_attrs__)

    assert declared == {"query", "query_more"}


def test_execution_calls_nothing_but_query_and_query_more():
    """AC6, at the call site."""
    client = FakeClient(
        batch("a", done=False, url="/q/1"),
        batch("b", done=False, url="/q/2"),
        batch("c", done=True),
    )

    execute(QUERY, client)

    assert set(client.method_names) == {"query", "query_more"}


def test_no_dml_verb_appears_in_the_execute_module():
    """Belt and braces: the source itself must not name a write method."""
    from pathlib import Path

    import soqlmodel.execute as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    for verb in ("bulk", ".create(", ".update(", ".upsert(", ".delete(", "DELETE ", "INSERT "):
        assert verb not in source, f"execute.py names {verb!r}"


# --- the optional dependency stays optional ----------------------------------


def test_importing_execute_does_not_import_simple_salesforce():
    """AC4. The Protocol is structural precisely so this holds."""
    import soqlmodel.execute  # noqa: F401

    assert "simple_salesforce" not in sys.modules


def test_the_query_is_rendered_exactly_as_query_py_renders_it():
    """execute adds nothing to the SOQL — no implicit LIMIT, no rewriting."""
    client = FakeClient(batch("a", done=True))
    query = QUERY.where(Account.Name == "Acme").limit(5)

    execute(query, client)

    assert client.calls[0][1][0] == query.render()
