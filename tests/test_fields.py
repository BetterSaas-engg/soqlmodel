import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

from soqlmodel.fields import Field, Filter, render_literal


class Account:
    """A hand-written stand-in for what the generator will emit."""

    Name: Field[str] = Field("Name", "xsd:string")
    AnnualRevenue: Field[float] = Field("AnnualRevenue", "xsd:double")
    CreatedDate: Field[datetime] = Field("CreatedDate", "xsd:dateTime")


def test_comparisons_build_filters_not_bools():
    assert isinstance(Account.AnnualRevenue > 1000000, Filter)
    assert isinstance(Account.AnnualRevenue >= 1000000, Filter)
    assert isinstance(Account.AnnualRevenue < 1000000, Filter)
    assert isinstance(Account.AnnualRevenue <= 1000000, Filter)
    assert isinstance(Account.Name == "Acme", Filter)


def test_not_equal_returns_a_filter_and_not_a_bool():
    # Python's derived __ne__ negates __eq__'s result and yields a bool. The
    # explicit __ne__ is what keeps this a Filter.
    result = Account.Name != "Acme"

    assert isinstance(result, Filter)
    assert not isinstance(result, bool)
    assert result.render() == "Name != 'Acme'"


@pytest.mark.parametrize(
    ("filter_", "expected"),
    [
        (Account.AnnualRevenue > 1000000, "AnnualRevenue > 1000000"),
        (Account.AnnualRevenue >= 1000000, "AnnualRevenue >= 1000000"),
        (Account.AnnualRevenue < 1000000, "AnnualRevenue < 1000000"),
        (Account.AnnualRevenue <= 1000000, "AnnualRevenue <= 1000000"),
        (Account.Name == "Acme", "Name = 'Acme'"),
        (Account.Name != "Acme", "Name != 'Acme'"),
    ],
)
def test_renders_soql_fragments(filter_, expected):
    assert filter_.render() == expected


# --- __bool__ is the safety mechanism ---------------------------------------

BOOL_MESSAGE = "Filter objects have no truth value"


def test_bool_raises():
    with pytest.raises(TypeError, match=BOOL_MESSAGE):
        bool(Account.Name == "Acme")


def test_bool_raises_in_an_if_statement():
    with pytest.raises(TypeError, match=BOOL_MESSAGE):
        if Account.Name == "Acme":
            pass


def test_bool_raises_when_chained_with_and():
    # `and` evaluates the left operand for truthiness — a common mistake,
    # since SOQL users reach for it to combine conditions.
    with pytest.raises(TypeError, match=BOOL_MESSAGE):
        _ = (Account.Name == "Acme") and (Account.AnnualRevenue > 1)


def test_bool_raises_on_membership_test():
    with pytest.raises(TypeError, match=BOOL_MESSAGE):
        _ = Account.Name in [Account.AnnualRevenue]


def test_bool_error_message_names_both_common_mistakes():
    with pytest.raises(TypeError) as exc:
        bool(Account.Name == "Acme")

    assert "if statement" in str(exc.value)
    assert "'in'" in str(exc.value)


# --- hashability ------------------------------------------------------------


def test_field_is_hashable():
    assert isinstance(hash(Account.Name), int)


def test_fields_are_usable_in_a_set():
    fields = {Account.Name, Account.AnnualRevenue, Account.CreatedDate}

    assert len(fields) == 3
    assert Account.Name in fields


def test_fields_are_usable_as_dict_keys():
    aliases = {Account.Name: "account_name", Account.AnnualRevenue: "revenue"}

    assert aliases[Account.Name] == "account_name"


def test_the_same_field_dedupes_normally():
    assert len({Account.Name, Account.Name}) == 1


def test_distinct_fields_sharing_a_name_are_not_set_equal():
    # Identity hashing (D7): two Field objects naming the same column are two
    # objects. They do not collapse, and crucially the set never falls back to
    # __eq__ — which would return a Filter and raise.
    fields = {Account.Name, Field("Name", "xsd:string")}

    assert len(fields) == 2


# --- literal rendering ------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Acme", "'Acme'"),
        ("O'Brien", r"'O\'Brien'"),
        (r"back\slash", r"'back\\slash'"),
        (None, "NULL"),
        (True, "true"),
        (False, "false"),
        (42, "42"),
        (1.5, "1.5"),
        (date(2026, 8, 9), "2026-08-09"),
        (
            datetime(2026, 8, 9, 13, 30, tzinfo=timezone.utc),
            "2026-08-09T13:30:00+00:00",
        ),
        (
            datetime(2026, 8, 9, 13, 30, tzinfo=timezone(timedelta(hours=2))),
            "2026-08-09T13:30:00+02:00",
        ),
    ],
)
def test_render_literal(value, expected):
    assert render_literal(value) == expected


def test_booleans_render_before_integers():
    # bool subclasses int; the isinstance order in render_literal matters.
    assert render_literal(True) == "true"
    assert render_literal(1) == "1"


def test_naive_datetime_is_refused():
    # Rendering it would emit SOQL the org rejects, surfacing the error at the
    # API boundary instead of at the mistake (D7).
    with pytest.raises(ValueError, match="naive datetime"):
        render_literal(datetime(2026, 8, 9, 13, 30))  # noqa: DTZ001


def test_naive_datetime_error_says_how_to_fix_it():
    with pytest.raises(ValueError, match="attach a timezone"):
        render_literal(datetime(2026, 8, 9, 13, 30))  # noqa: DTZ001


def test_naive_datetime_is_refused_through_a_comparison():
    with pytest.raises(ValueError, match="naive datetime"):
        (Account.CreatedDate > datetime(2026, 8, 9, 13, 30)).render()  # noqa: DTZ001


def test_dates_are_unaffected_by_the_timezone_rule():
    # A date has no time and no offset; SOQL takes it bare.
    assert render_literal(date(2026, 8, 9)) == "2026-08-09"


# --- the type checker is part of the contract -------------------------------


def test_mypy_passes_clean_on_the_module():
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "src/soqlmodel/fields.py"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
