import subprocess
import sys
from datetime import UTC, datetime

import pytest

from soqlmodel.fields import Composite, Field, escape_like_wildcards, escape_string
from soqlmodel.query import Query, select


class Account:
    Id: Field[str] = Field("Id", "id")
    Name: Field[str] = Field("Name", "string")
    AnnualRevenue: Field[float] = Field("AnnualRevenue", "currency")
    CreatedDate: Field[datetime] = Field("CreatedDate", "datetime")


class Contact:
    Name: Field[str] = Field("Name", "string")
    Email: Field[str] = Field("Email", "email")
    # Shared with Account, same Python type. The dangerous case: using this on
    # an Account query type-checks, renders as valid SOQL, and would silently
    # filter on the wrong object.
    CreatedDate: Field[datetime] = Field("CreatedDate", "datetime")


# --- rendering --------------------------------------------------------------


def test_renders_the_documented_example():
    query = (
        select(Account, Account.Name, Account.AnnualRevenue)
        .where(Account.AnnualRevenue > 1000000)
        .order_by(Account.Name)
        .limit(50)
    )

    assert str(query) == (
        "SELECT Name, AnnualRevenue FROM Account "
        "WHERE AnnualRevenue > 1000000 "
        "ORDER BY Name LIMIT 50"
    )


def test_renders_without_optional_clauses():
    assert str(select(Account, Account.Name)) == "SELECT Name FROM Account"


def test_orders_by_several_fields():
    query = select(Account, Account.Name).order_by(Account.Name, Account.AnnualRevenue)

    assert "ORDER BY Name, AnnualRevenue" in str(query)


def test_orders_descending():
    query = select(Account, Account.Name).order_by(Account.Name, desc=True)

    assert str(query).endswith("ORDER BY Name DESC")


def test_desc_applies_to_every_field_in_the_call():
    query = select(Account, Account.Name).order_by(Account.Name, Account.AnnualRevenue, desc=True)

    assert "ORDER BY Name DESC, AnnualRevenue DESC" in str(query)


def test_directions_mix_by_chaining():
    query = (
        select(Account, Account.Name)
        .order_by(Account.AnnualRevenue, desc=True)
        .order_by(Account.Name)
    )

    assert "ORDER BY AnnualRevenue DESC, Name" in str(query)


def test_ascending_is_the_default():
    query = select(Account, Account.Name).order_by(Account.Name)

    assert "DESC" not in str(query)


def test_desc_does_not_mutate_the_original():
    base = select(Account, Account.Name).order_by(Account.Name)
    derived = base.order_by(Account.AnnualRevenue, desc=True)

    assert "DESC" not in str(base)
    assert "DESC" in str(derived)


def test_successive_where_calls_and_together():
    query = (
        select(Account, Account.Name).where(Account.Name == "Acme").where(Account.AnnualRevenue > 1)
    )

    assert "WHERE (Name = 'Acme') AND (AnnualRevenue > 1)" in str(query)


def test_repr_shows_the_query():
    assert repr(select(Account, Account.Name)) == "Query('SELECT Name FROM Account')"


# --- combining conditions ---------------------------------------------------


def test_and_renders_both_sides_parenthesized():
    condition = (Account.Name == "Acme") & (Account.AnnualRevenue > 1000)

    assert isinstance(condition, Composite)
    assert condition.render() == "(Name = 'Acme') AND (AnnualRevenue > 1000)"


def test_or_renders_both_sides_parenthesized():
    condition = (Account.Name == "Acme") | (Account.Name == "Initech")

    assert condition.render() == "(Name = 'Acme') OR (Name = 'Initech')"


def test_nested_combinations_keep_their_shape():
    condition = ((Account.Name == "A") | (Account.Name == "B")) & (Account.AnnualRevenue > 10)

    assert condition.render() == ("((Name = 'A') OR (Name = 'B')) AND (AnnualRevenue > 10)")


def test_and_keyword_still_raises():
    # `and` evaluates truthiness; `&` is the operator that works.
    with pytest.raises(TypeError, match="no truth value"):
        _ = (Account.Name == "Acme") and (Account.AnnualRevenue > 1)


def test_the_error_message_points_at_the_operators():
    with pytest.raises(TypeError, match=r"& and \|"):
        bool(Account.Name == "Acme")


# --- immutability -----------------------------------------------------------


def test_where_does_not_mutate_the_original():
    base = select(Account, Account.Name)
    derived = base.where(Account.AnnualRevenue > 1)

    assert str(base) == "SELECT Name FROM Account"
    assert derived is not base
    assert "WHERE" in str(derived)


def test_order_by_does_not_mutate_the_original():
    base = select(Account, Account.Name)
    derived = base.order_by(Account.Name)

    assert "ORDER BY" not in str(base)
    assert "ORDER BY" in str(derived)


def test_limit_does_not_mutate_the_original():
    base = select(Account, Account.Name)
    derived = base.limit(10)

    assert "LIMIT" not in str(base)
    assert "LIMIT 10" in str(derived)


def test_a_base_query_can_be_forked_twice_independently():
    base = select(Account, Account.Name).where(Account.AnnualRevenue > 0)
    big = base.where(Account.AnnualRevenue > 1000000)
    named = base.where(Account.Name == "Acme")

    assert "1000000" not in str(named)
    assert "Acme" not in str(big)
    assert str(base) == "SELECT Name FROM Account WHERE AnnualRevenue > 0"


def test_query_has_no_public_mutable_state():
    query = select(Account, Account.Name)

    with pytest.raises(AttributeError):
        query.something = 1  # type: ignore[attr-defined]


# --- field ownership --------------------------------------------------------


def test_rejects_a_field_from_another_sobject_in_select():
    with pytest.raises(ValueError, match="not a field of Account"):
        select(Account, Contact.Email)


def test_rejects_a_same_named_field_from_another_sobject():
    # Contact.Name and Account.Name share a name; identity tells them apart.
    with pytest.raises(ValueError, match="not a field of Account"):
        select(Account, Contact.Name)


def test_rejects_a_foreign_field_in_order_by():
    with pytest.raises(ValueError, match="not a field of Account"):
        select(Account, Account.Name).order_by(Contact.Email)


def test_rejects_a_condition_on_a_foreign_field():
    with pytest.raises(ValueError, match="not a field of Account"):
        select(Account, Account.Name).where(Contact.Email == "a@b.com")


def test_rejects_a_foreign_field_inside_a_composite():
    with pytest.raises(ValueError, match="not a field of Account"):
        select(Account, Account.Name).where((Account.Name == "Acme") & (Contact.Email == "a@b.com"))


def test_rejects_a_shared_field_name_from_another_sobject_in_where():
    """The silent wrong-answer case that name-based checking let through.

    Account and Contact both have CreatedDate, both Field[datetime]. mypy is
    happy, the name check was happy, and the query rendered as valid SOQL that
    filtered the wrong object's column. Identity catches it (D8).
    """
    when = datetime(2026, 8, 9, tzinfo=UTC)

    with pytest.raises(ValueError, match="not a field of Account"):
        select(Account, Account.Name).where(Contact.CreatedDate > when)


def test_the_correct_sobjects_shared_field_is_still_accepted():
    when = datetime(2026, 8, 9, tzinfo=UTC)
    query = select(Account, Account.Name).where(Account.CreatedDate > when)

    assert "WHERE CreatedDate > 2026-08-09T00:00:00+00:00" in str(query)


def test_rejects_a_shared_field_name_nested_in_a_composite():
    when = datetime(2026, 8, 9, tzinfo=UTC)

    with pytest.raises(ValueError, match="not a field of Account"):
        select(Account, Account.Name).where((Account.Name == "Acme") & (Contact.CreatedDate > when))


def test_the_error_lists_the_known_fields():
    with pytest.raises(ValueError, match="Known fields: AnnualRevenue, CreatedDate, Id, Name"):
        select(Account, Contact.Email)


def test_select_needs_at_least_one_field():
    with pytest.raises(ValueError, match="at least one field"):
        select(Account)


def test_rejects_a_non_field_in_select():
    with pytest.raises(TypeError, match="expected a Field"):
        select(Account, "Name")  # type: ignore[arg-type]


def test_rejects_a_non_condition_in_where():
    with pytest.raises(TypeError, match="expected a filter condition"):
        select(Account, Account.Name).where("Name = 'Acme'")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, -50])
def test_limit_must_be_positive(bad):
    with pytest.raises(ValueError, match="must be positive"):
        select(Account, Account.Name).limit(bad)


@pytest.mark.parametrize("bad", [1.5, "10", True, None])
def test_limit_must_be_an_int(bad):
    with pytest.raises(TypeError, match="must be an int"):
        select(Account, Account.Name).limit(bad)


# ============================================================================
# INJECTION
#
# Every test here asserts the payload lands as inert data: the quotes that
# delimit the literal are the only unescaped quotes in the rendered query.
# ============================================================================


def literal_of(query: Query) -> str:
    """The single-quoted literal from a rendered query's WHERE clause."""
    rendered = str(query)
    start = rendered.index("'")
    return rendered[start:]


def test_single_quote_is_escaped():
    query = select(Account, Account.Name).where(Account.Name == "O'Brien")

    assert str(query).endswith(r"WHERE Name = 'O\'Brien'")


def test_string_termination_attempt_is_inert():
    payload = "Acme' OR Name != '"
    query = select(Account, Account.Name).where(Account.Name == payload)

    rendered = str(query)
    assert rendered == r"SELECT Name FROM Account WHERE Name = 'Acme\' OR Name != \''"
    # Every quote inside the literal is escaped, so the OR is data.
    assert "' OR " not in rendered.replace(r"\'", "")


def test_classic_or_1_equals_1_is_inert():
    payload = "' OR 1=1 --"
    query = select(Account, Account.Name).where(Account.Name == payload)

    assert str(query) == r"SELECT Name FROM Account WHERE Name = '\' OR 1=1 --'"


def test_backslash_is_doubled_so_it_cannot_escape_the_closing_quote():
    # The dangerous case: a trailing backslash that would otherwise escape the
    # literal's own closing quote and let the next characters run as SOQL.
    payload = "Acme\\"
    query = select(Account, Account.Name).where(Account.Name == payload)

    assert str(query) == r"SELECT Name FROM Account WHERE Name = 'Acme\\'"


def test_backslash_quote_pair_cannot_smuggle_a_quote():
    payload = "\\' OR Name != '"
    rendered = str(select(Account, Account.Name).where(Account.Name == payload))

    assert rendered == r"SELECT Name FROM Account WHERE Name = '\\\' OR Name != \''"


def test_newline_and_carriage_return_are_escaped():
    payload = "line1\nline2\rline3"
    rendered = str(select(Account, Account.Name).where(Account.Name == payload))

    assert r"'line1\nline2\rline3'" in rendered
    assert "\n" not in rendered
    assert "\r" not in rendered


def test_tab_backspace_and_formfeed_are_escaped():
    payload = "a\tb\bc\fd"
    rendered = str(select(Account, Account.Name).where(Account.Name == payload))

    assert r"'a\tb\bc\fd'" in rendered


def test_double_quote_is_escaped():
    rendered = str(select(Account, Account.Name).where(Account.Name == 'say "hi"'))

    assert r"'say \"hi\"'" in rendered


def test_undocumented_control_characters_become_unicode_escapes():
    payload = "a\x00b\x1fc\x7fd"
    rendered = str(select(Account, Account.Name).where(Account.Name == payload))

    assert r"'a\u0000b\u001fc\u007fd'" in rendered
    assert "\x00" not in rendered


def test_unicode_passes_through_as_itself():
    # Non-ASCII is data, not a threat; SOQL is UTF-8 and escaping it would
    # only make queries unreadable.
    rendered = str(select(Account, Account.Name).where(Account.Name == "Größe 日本 café"))

    assert "'Größe 日本 café'" in rendered


def test_unicode_escape_sequence_in_the_payload_is_neutralized():
    # A payload that tries to smuggle a quote as SOQL's own \uXXXX sequence.
    # Built by concatenation so the source is unambiguous: backslash, then
    # the literal text "u0027 OR 1=1".
    payload = "\\" + "u0027 OR 1=1"
    rendered = str(select(Account, Account.Name).where(Account.Name == payload))

    assert rendered == r"SELECT Name FROM Account WHERE Name = '\\u0027 OR 1=1'"


def test_payload_in_a_composite_is_escaped_on_both_sides():
    rendered = str(
        select(Account, Account.Name).where(
            (Account.Name == "a' OR '1'='1") | (Account.Id == "b' OR '1'='1")
        )
    )

    assert rendered.count(r"\'") == 8
    assert "' OR '" not in rendered.replace(r"\'", "")


def test_escaping_lives_in_one_place():
    # query.py must not grow a second implementation: the rendered literal is
    # exactly what escape_string produces.
    payload = 'O\'Brien\\\n"x"'
    rendered = str(select(Account, Account.Name).where(Account.Name == payload))

    assert rendered.endswith(f"'{escape_string(payload)}'")


# --- LIKE and its wildcards -------------------------------------------------


def test_like_treats_wildcards_as_intentional():
    query = select(Account, Account.Name).where(Account.Name.like("Acme%"))

    assert str(query).endswith("WHERE Name LIKE 'Acme%'")


def test_like_still_escapes_quotes():
    query = select(Account, Account.Name).where(Account.Name.like("O'Brien%"))

    assert str(query).endswith(r"WHERE Name LIKE 'O\'Brien%'")


def test_escape_like_wildcards_makes_them_literal():
    assert escape_like_wildcards("100%_off") == r"100\%\_off"


def test_escaped_wildcards_survive_rendering():
    pattern = escape_like_wildcards("100%")
    query = select(Account, Account.Name).where(Account.Name.like(pattern))

    # The backslash is doubled by string escaping; SOQL unescapes one layer.
    assert str(query).endswith(r"WHERE Name LIKE '100\\%'")


# --- other literal types ----------------------------------------------------


def test_datetime_literals_are_unquoted():
    when = datetime(2026, 8, 9, 13, 30, tzinfo=UTC)
    query = select(Account, Account.Name).where(Account.CreatedDate > when)

    assert str(query).endswith("WHERE CreatedDate > 2026-08-09T13:30:00+00:00")


def test_null_renders_unquoted():
    query = select(Account, Account.Name).where(Account.Name == None)

    assert str(query).endswith("WHERE Name = NULL")


# --- the type checker -------------------------------------------------------


def test_mypy_passes_clean_on_the_module():
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "src/soqlmodel/query.py"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
