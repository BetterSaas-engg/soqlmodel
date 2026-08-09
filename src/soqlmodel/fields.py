"""Field descriptors and the filters their comparison operators build.

A generated model class is a PURE SCHEMA DESCRIPTOR: it is never instantiated
and never holds row data. ``Account.Name`` is a :class:`Field` describing a
column, so ``Account.Name == "Acme"`` builds a WHERE fragment rather than
comparing two values.

Every field is a plain annotated class attribute, so mypy and Pylance resolve
them statically — no metaclasses, no ``__getattr__``. Attribute typos and
type-mismatched comparisons are caught by the type checker (D7).
"""

from datetime import date, datetime
from typing import Generic, TypeVar

T = TypeVar("T")


class Filter:
    """One rendered SOQL comparison, e.g. ``AnnualRevenue > 1000000``."""

    __slots__ = ("field", "operator", "value")

    def __init__(self, field: str, operator: str, value: object) -> None:
        self.field = field
        self.operator = operator
        self.value = value

    def render(self) -> str:
        """Render as a SOQL WHERE fragment."""
        return f"{self.field} {self.operator} {render_literal(self.value)}"

    def __bool__(self) -> bool:
        """Always raises. This is the safety mechanism, not a formality (D7).

        A filter describes a comparison to send to Salesforce; it has no truth
        value here. Anything that evaluates one for truthiness — an ``if``, an
        ``and``/``or`` chain, ``in``, ``sorted()``, ``assert`` — is a mistake
        that would otherwise pass silently and produce a wrong query. Raising
        turns each of those into a loud error at the point of the mistake.
        """
        raise TypeError(
            "Filter objects have no truth value. Did you use a filter in an "
            "if statement, or compare with 'in'?"
        )

    def __repr__(self) -> str:
        return f"Filter({self.render()!r})"


def render_literal(value: object) -> str:
    """Render a Python value as a SOQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        # Before int: bool is a subclass of int.
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    if isinstance(value, datetime):
        # SOQL datetime literals are unquoted ISO-8601 and require an offset.
        # Refuse to render something the org would reject: the error belongs
        # at the mistake, not at the API boundary (D7).
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                f"naive datetime {value.isoformat()!r} cannot be rendered as a SOQL "
                "literal; attach a timezone, e.g. "
                "value.replace(tzinfo=timezone.utc)"
            )
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


class Field(Generic[T]):
    """Describes one column of one sobject.

    The type parameter is the Python type the column holds, so comparing a
    ``Field[str]`` against an ``int`` is a type error rather than a query that
    fails at the org.
    """

    __slots__ = ("name", "soap_type")

    def __init__(self, name: str, soap_type: str) -> None:
        self.name = name
        self.soap_type = soap_type

    # object defines neither __gt__ nor its siblings, so these override
    # nothing and need no ignore.
    def __gt__(self, other: T) -> Filter:
        return Filter(self.name, ">", other)

    def __ge__(self, other: T) -> Filter:
        return Filter(self.name, ">=", other)

    def __lt__(self, other: T) -> Filter:
        return Filter(self.name, "<", other)

    def __le__(self, other: T) -> Filter:
        return Filter(self.name, "<=", other)

    # __eq__ and __ne__ DO override object, which types them as
    # (object) -> bool. Returning a Filter from a narrowed parameter violates
    # Liskov, and mypy is right to say so — but the violation is the whole
    # point of the design, and it is the same trade-off SQLAlchemy makes for
    # its column expressions. Silenced deliberately, per operator, not
    # module-wide (D7).
    def __eq__(self, other: T) -> Filter:  # type: ignore[override]
        return Filter(self.name, "=", other)

    def __ne__(self, other: T) -> Filter:  # type: ignore[override]
        # Defined explicitly: Python's derived __ne__ negates __eq__'s result,
        # which would call Filter.__bool__ and raise. Users expect != to work.
        return Filter(self.name, "!=", other)

    # Defining __eq__ sets __hash__ to None, which would make Field unusable in
    # the sets and dicts the query builder needs. Restore identity hashing
    # rather than hashing the name: a generated model holds exactly one Field
    # per column, so identity dedupes correctly for every real use, and name
    # hashing would make two same-named Fields hash equal, sending the set to
    # __eq__ — which returns a Filter, whose __bool__ raises. SQLAlchemy does
    # the same for the same reason. Name-level dedupe is the query builder's
    # job, done explicitly (D7).
    __hash__ = object.__hash__

    def __repr__(self) -> str:
        return f"Field({self.name!r}, {self.soap_type!r})"
