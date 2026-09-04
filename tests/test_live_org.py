"""The last gap: simple-salesforce answering our two calls over real HTTP.

SFM-11 stubbed ``_call_salesforce`` and let the genuine ``Salesforce.query``
and ``.query_more`` run our drain. That covered parameter names, the
``identifier_is_url`` URL branch and response parsing — everything except the
wire. SFM-10 covered the wire, but through an ``sf``-CLI-backed client that
merely satisfies the same Protocol. Neither run had simple-salesforce talking
to Salesforce.

These tests close that, and nothing else. Every test here can fail for reasons
that have nothing to do with this codebase — an expired cert, a sandbox
refresh, a dropped connection — so the set is deliberately three tests, one per
thing that was actually unproven. Field types, WHERE clauses, LIMIT and the
malformed-response branches are covered exhaustively offline in
test_execute.py; running them again over HTTP would buy no information and add
three more ways for a green suite to go red.

## Opting in

Off unless ``SOQLMODEL_LIVE_ORG=1``. That switch, not the presence of
credentials, is what enables the module — so credentials sitting in a shell
profile can never turn the normal suite into one that opens sockets.

Once opted in, *nothing here skips*. Missing credentials, a missing extra, an
object that no longer pages: all failures. This is the SFM-11 property — an
opted-in run must not be able to pass by quietly running nothing — collapsed
into one variable, because unlike a missing optional extra there is no
legitimate reason to ask for a live run and accept zero live tests.

## Credentials

Environment only. Nothing in this repo names the org, the user, the app or the
key, and no message below echoes a value — only the variable that was empty.

    SOQLMODEL_LIVE_ORG=1                       opt in
    SOQLMODEL_LIVE_USERNAME=...                JWT subject
    SOQLMODEL_LIVE_CONSUMER_KEY=...            connected app consumer key
    SOQLMODEL_LIVE_PRIVATEKEY_FILE=...         path to the private key, outside the repo
    SOQLMODEL_LIVE_DOMAIN=...                  e.g. "example--sandbox.my"
    SOQLMODEL_LIVE_PAGING_OBJECT=Contact       optional; must exceed one batch
    SOQLMODEL_LIVE_SF_ALIAS=...                sf CLI alias for the SAME org, for the
                                               two-source comparison (SFM-13c)
    SOQLMODEL_LIVE_API_VERSION=68.0            optional; pins both sources
"""

import os
from typing import Any

import pytest

pytestmark = pytest.mark.live

OPT_IN = "SOQLMODEL_LIVE_ORG"
CREDENTIALS = (
    "SOQLMODEL_LIVE_USERNAME",
    "SOQLMODEL_LIVE_CONSUMER_KEY",
    "SOQLMODEL_LIVE_PRIVATEKEY_FILE",
    "SOQLMODEL_LIVE_DOMAIN",
)

# A Salesforce query returns at most this many rows per batch. Crossing it is
# the only way to prove the drain is doing anything.
BATCH_SIZE = 2000

if os.environ.get(OPT_IN) != "1":
    pytest.skip(
        f"live-org tests are opt-in; set {OPT_IN}=1 (and see this module's docstring "
        f"for the credentials it then requires)",
        allow_module_level=True,
    )

# Opted in. Everything from here is a hard failure, never a skip.
_missing = [name for name in CREDENTIALS if not os.environ.get(name)]
if _missing:
    raise RuntimeError(
        f"{OPT_IN}=1 asks for a live run, but these environment variables are "
        f"unset or empty: {', '.join(_missing)}. Refusing to skip: an opted-in "
        f"run that silently tests nothing is the failure this gate exists to prevent."
    )

# Deliberately below the gate: with the extra absent, this ImportError is a
# collection error and the run fails, which is what an opted-in run deserves.
from simple_salesforce import Salesforce

from soqlmodel.describe import build_snapshot
from soqlmodel.execute import execute, execute_iter
from soqlmodel.extract import SOURCE_CREDENTIALS, SOURCE_SF, extract_describe
from soqlmodel.fields import Field
from soqlmodel.query import select

PAGING_OBJECT = os.environ.get("SOQLMODEL_LIVE_PAGING_OBJECT", "Contact")

# The object name is the class name (Query.render reads ``model.__name__``), so
# a configurable object means a dynamically built model. Generated models are
# written to a file; this one only needs to satisfy the same shape.
PagingModel: Any = type(PAGING_OBJECT, (), {"Id": Field("Id", "id")})


@pytest.fixture(scope="session")
def live_org() -> Salesforce:
    """A real ``Salesforce``, authenticated by JWT bearer flow.

    Session-scoped: one token exchange for the module. The object handed to
    ``execute`` below is this one, untouched.
    """
    return Salesforce(
        username=os.environ["SOQLMODEL_LIVE_USERNAME"],
        consumer_key=os.environ["SOQLMODEL_LIVE_CONSUMER_KEY"],
        privatekey_file=os.environ["SOQLMODEL_LIVE_PRIVATEKEY_FILE"],
        domain=os.environ["SOQLMODEL_LIVE_DOMAIN"],
    )


@pytest.fixture
def watched(live_org: Salesforce) -> Any:
    """Records every HTTP round trip the client makes, and delegates.

    An observer, not a stub: the genuine ``_call_salesforce`` still runs and
    still goes to Salesforce. This is the only way to assert *when* a request
    happened, which is what laziness means.
    """
    calls: list[dict[str, Any]] = []
    original = live_org._call_salesforce

    def recording(method: str, url: str, name: str | None = None, **kwargs: Any) -> Any:
        # Appended before the call, so a request that is made but fails still
        # shows up. Laziness is a claim about requests attempted.
        entry: dict[str, Any] = {"name": name, "rows": None}
        calls.append(entry)
        response = original(method, url, name=name, **kwargs)
        entry["rows"] = len(response.json().get("records", []))
        return response

    live_org._call_salesforce = recording
    try:
        yield calls
    finally:
        live_org._call_salesforce = original


def total_rows(client: Salesforce, sobject: str) -> int:
    """The org's own count, used as the oracle.

    Not a hardcoded number: row counts drift, and a test asserting last
    quarter's total would fail for a reason that is not a defect.
    """
    return int(client.query(f"SELECT COUNT() FROM {sobject}")["totalSize"])


def test_a_real_salesforce_executes_our_query_over_http(live_org: Salesforce) -> None:
    """AC1. JWT-authenticated simple-salesforce, our ``execute``, real rows.

    Kept separate from the paging test so that a broken credential fails here
    and says so, rather than surfacing as a confusing paging failure.
    """
    query = select(PagingModel, PagingModel.Id).limit(5)

    rows = execute(query, live_org)

    assert len(rows) == 5
    assert all(row["Id"] for row in rows)


def test_the_drain_crosses_a_real_batch_boundary(
    live_org: Salesforce, watched: list[dict[str, Any]]
) -> None:
    """AC2. More than one batch, over the wire, through ``query_more``.

    The assertion is against the org's own ``COUNT()``. Anything short of it is
    the exact failure this project exists to prevent: a plausible number in
    place of the answer.
    """
    expected = total_rows(live_org, PAGING_OBJECT)
    if expected <= BATCH_SIZE:
        pytest.fail(
            f"{PAGING_OBJECT} has {expected} rows, which fits in one batch of "
            f"{BATCH_SIZE}, so this test would prove nothing. Set "
            f"SOQLMODEL_LIVE_PAGING_OBJECT to an object that pages."
        )
    watched.clear()  # the COUNT() above is the oracle, not part of the drain

    rows = execute(select(PagingModel, PagingModel.Id), live_org)

    assert len(rows) == expected

    names = [call["name"] for call in watched]
    assert names[0] == "query"
    assert set(names[1:]) == {"query_more"}, f"unexpected calls in the drain: {names}"
    assert len(names) > 1, "no query_more was issued, so no boundary was crossed"

    batches = [call["rows"] for call in watched]
    print(f"\n{PAGING_OBJECT}: {expected} rows drained in {len(batches)} batches {batches}")


def test_execute_iter_stays_lazy_over_http(
    live_org: Salesforce, watched: list[dict[str, Any]]
) -> None:
    """AC3. Building the iterator does no I/O; one row costs one batch.

    Run against the paging object on purpose: proving that one row fetched one
    batch out of several is a stronger claim than proving it against a result
    that only ever had one.
    """
    stream = execute_iter(select(PagingModel, PagingModel.Id), live_org)
    assert watched == [], "constructing the iterator went to the network"

    first = next(stream)

    assert first["Id"]
    assert len(watched) == 1, f"one row should cost one batch, not {len(watched)}"

    # Abandon the cursor rather than draining thousands of rows we do not need.
    stream.close()
    assert len(watched) == 1, "closing the iterator fetched more"


# --- the credential extractor against the real org (SFM-13c) ------------------
#
# SFM-13a compared the two extraction sources by hand and found them identical
# across every consumed key. This is that comparison made permanent: the same
# claim, re-checked on demand instead of remembered from a report.

SF_ALIAS = os.environ.get("SOQLMODEL_LIVE_SF_ALIAS")
API_VERSION = os.environ.get("SOQLMODEL_LIVE_API_VERSION", "68.0")


@pytest.mark.parametrize("sobject", ["Account", "Contact"])
def test_both_sources_build_the_same_snapshot(sobject: str) -> None:
    """The credential path and the sf path, same org, same pinned version.

    Byte-identical snapshots or the credential extractor is not a drop-in
    replacement, whatever the field-by-field tables say.

    Both are fetched in one test rather than in two, so a schema change cannot
    land between them and be misread as a source difference.
    """
    if not SF_ALIAS:
        pytest.fail(
            "SOQLMODEL_LIVE_SF_ALIAS is unset, so the sf side of this comparison "
            "cannot run. Set it to the sf CLI alias for the same org the "
            "SOQLMODEL_LIVE_* credentials point at. Refusing to skip: an "
            "opted-in run that quietly compares nothing is the failure this gate exists to prevent."
        )

    # The credential extractor reads SOQLMODEL_SF_*, while this module's gate
    # uses SOQLMODEL_LIVE_*. Bridging them here rather than reusing one set of
    # names is the point of keeping them distinct -- see D21.
    for target, live in (
        ("SOQLMODEL_SF_USERNAME", "SOQLMODEL_LIVE_USERNAME"),
        ("SOQLMODEL_SF_CONSUMER_KEY", "SOQLMODEL_LIVE_CONSUMER_KEY"),
        ("SOQLMODEL_SF_PRIVATEKEY_FILE", "SOQLMODEL_LIVE_PRIVATEKEY_FILE"),
        ("SOQLMODEL_SF_DOMAIN", "SOQLMODEL_LIVE_DOMAIN"),
    ):
        os.environ[target] = os.environ[live]

    via_sf = extract_describe(sobject, org=SF_ALIAS, source=SOURCE_SF, api_version=API_VERSION)
    via_credentials = extract_describe(
        sobject, org=SF_ALIAS, source=SOURCE_CREDENTIALS, api_version=API_VERSION
    )

    snapshot_sf = build_snapshot(via_sf, org=SF_ALIAS)
    snapshot_credentials = build_snapshot(via_credentials, org=SF_ALIAS)

    assert snapshot_sf["fields"] == snapshot_credentials["fields"]
    assert snapshot_sf == snapshot_credentials

    print(
        f"\n{sobject}: {len(snapshot_sf['fields'])} fields, "
        f"identical from both sources at API {API_VERSION}"
    )
