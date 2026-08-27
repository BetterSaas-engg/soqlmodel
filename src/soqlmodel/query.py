"""Stage 4: build SOQL strings from generated models.

A :class:`Query` is immutable. Every method returns a new Query, so a base
query can be forked without the branches interfering::

    base = select(Account, Account.Name).where(Account.AnnualRevenue > 0)
    big = base.where(Account.AnnualRevenue > 1_000_000)   # base is unchanged

Conditions combine with ``&`` and ``|``, and **each side must be
parenthesized**::

    .where((Account.Name == "Acme") & (Account.AnnualRevenue > 1000))

Python binds ``&`` tighter than ``>``, so the parentheses are not stylistic —
without them the expression is a different, usually nonsensical, one. This is
SQLAlchemy's constraint for the same reason; we document it rather than try to
defeat Python's precedence (D8). Using ``and``/``or`` raises, by design.

Pure: no network here. A Query renders to a string; sending it is someone
else's job.
"""

from typing import Any

from soqlmodel.fields import Condition, Field


def _model_fields(model: type) -> dict[str, Field[Any]]:
    """The Field descriptors declared on a generated model class."""
    return {name: value for name, value in vars(model).items() if isinstance(value, Field)}


class Query:
    """An immutable SOQL SELECT under construction."""

    __slots__ = ("_conditions", "_fields", "_limit", "_model", "_order_by")

    def __init__(
        self,
        model: type,
        fields: tuple[Field[Any], ...],
        conditions: tuple[Condition, ...] = (),
        order_by: tuple[tuple[Field[Any], bool], ...] = (),
        limit: int | None = None,
    ) -> None:
        self._model = model
        self._fields = fields
        self._conditions = conditions
        self._order_by = order_by
        self._limit = limit

    # --- construction -------------------------------------------------------

    def _replace(self, **changes: Any) -> "Query":
        """Build a new Query, changing only what is named. Never mutates."""
        return Query(
            model=changes.get("model", self._model),
            fields=changes.get("fields", self._fields),
            conditions=changes.get("conditions", self._conditions),
            order_by=changes.get("order_by", self._order_by),
            limit=changes.get("limit", self._limit),
        )

    def where(self, *conditions: Condition) -> "Query":
        """Add conditions. Successive calls AND together."""
        for condition in conditions:
            self._check_condition(condition)
        return self._replace(conditions=self._conditions + conditions)

    def order_by(self, *fields: Field[Any], desc: bool = False) -> "Query":
        """Order by one or more fields. Ascending unless ``desc=True``.

        ``desc`` applies to every field in this call. Mix directions by
        chaining: ``.order_by(A, desc=True).order_by(B)``.
        """
        for field in fields:
            self._check_field(field)
        added = tuple((field, desc) for field in fields)
        return self._replace(order_by=self._order_by + added)

    def limit(self, count: int) -> "Query":
        """Limit the row count."""
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError(f"limit must be an int, got {type(count).__name__}")
        if count < 1:
            raise ValueError(f"limit must be positive, got {count}")
        return self._replace(limit=count)

    # --- validation ---------------------------------------------------------

    def _check_field(self, field: Field[Any]) -> None:
        """Reject a Field that is not declared on this query's model.

        Checked by identity, so a same-named field from a different sObject is
        caught too — ``Contact.Name`` on an Account query is an error, not a
        coincidence.
        """
        if not isinstance(field, Field):
            raise TypeError(f"expected a Field, got {type(field).__name__}: {field!r}")

        declared = _model_fields(self._model)
        if not any(field is candidate for candidate in declared.values()):
            raise ValueError(
                f"{field.name!r} is not a field of {self._model.__name__}. "
                f"Known fields: {', '.join(sorted(declared)) or '(none)'}"
            )

    def _check_condition(self, condition: Condition) -> None:
        """Reject a condition referencing a field this model does not have.

        Checked by identity, exactly as :meth:`_check_field` does: a condition
        carries the Field objects it was built from, so ``Contact.CreatedDate``
        on an Account query is caught even though Account has a CreatedDate of
        its own (D8).
        """
        if not isinstance(condition, Condition):
            raise TypeError(
                f"expected a filter condition, got {type(condition).__name__}: {condition!r}"
            )

        for field in sorted(condition.fields(), key=lambda f: f.name):
            self._check_field(field)

    # --- rendering ----------------------------------------------------------

    def render(self) -> str:
        """Render the SOQL string."""
        columns = ", ".join(field.name for field in self._fields)
        parts = [f"SELECT {columns}", f"FROM {self._model.__name__}"]

        if self._conditions:
            joined = " AND ".join(
                condition.render() if len(self._conditions) == 1 else f"({condition.render()})"
                for condition in self._conditions
            )
            parts.append(f"WHERE {joined}")

        if self._order_by:
            terms = ", ".join(
                f"{field.name} DESC" if desc else field.name for field, desc in self._order_by
            )
            parts.append(f"ORDER BY {terms}")

        if self._limit is not None:
            parts.append(f"LIMIT {self._limit}")

        return " ".join(parts)

    def __str__(self) -> str:
        return self.render()

    def __repr__(self) -> str:
        return f"Query({self.render()!r})"


def select(model: type, *fields: Field[Any]) -> Query:
    """Start a query against ``model``, selecting ``fields``.

    Raises:
        ValueError: if no fields are given, or a field is not declared on the
            model.
    """
    if not fields:
        raise ValueError(f"select({model.__name__}) needs at least one field; SOQL has no SELECT *")

    query = Query(model=model, fields=fields)
    for field in fields:
        query._check_field(field)
    return query
