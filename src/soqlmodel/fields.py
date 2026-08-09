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
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class Condition:
    """A renderable WHERE condition: a :class:`Filter` or a combination.

    Combine with ``&`` and ``|``, never with ``and``/``or`` — the keywords
    evaluate truthiness, which raises by design.
    """

    __slots__ = ()

    def render(self) -> str:
        raise NotImplementedError

    def fields(self) -> frozenset["Field[Any]"]:
        """Every Field this condition references.

        Field *objects*, not names, so a caller can check ownership by
        identity — two sObjects both having a ``CreatedDate`` is the common
        case, not the exotic one (D8).
        """
        raise NotImplementedError

    def __and__(self, other: "Condition") -> "Composite":
        return Composite("AND", self, other)

    def __or__(self, other: "Condition") -> "Composite":
        return Composite("OR", self, other)

    def __bool__(self) -> bool:
        """Always raises. This is the safety mechanism, not a formality (D7).

        A condition describes a comparison to send to Salesforce; it has no
        truth value here. Anything that evaluates one for truthiness — an
        ``if``, an ``and``/``or`` chain, ``in``, ``sorted()``, ``assert`` — is
        a mistake that would otherwise pass silently and produce a wrong
        query. Raising turns each of those into a loud error at the point of
        the mistake.
        """
        raise TypeError(
            "Filter objects have no truth value. Did you use a filter in an "
            "if statement, or compare with 'in'? Combine conditions with "
            "& and |, and parenthesize each side."
        )


class Filter(Condition):
    """One rendered SOQL comparison, e.g. ``AnnualRevenue > 1000000``."""

    __slots__ = ("field", "operator", "value")

    def __init__(self, field: "Field[Any]", operator: str, value: object) -> None:
        self.field = field
        self.operator = operator
        self.value = value

    def render(self) -> str:
        """Render as a SOQL WHERE fragment."""
        return f"{self.field.name} {self.operator} {render_literal(self.value)}"

    def fields(self) -> frozenset["Field[Any]"]:
        return frozenset({self.field})

    def __repr__(self) -> str:
        return f"Filter({self.render()!r})"


class Composite(Condition):
    """Two conditions joined by AND or OR.

    Each side is rendered inside its own parentheses. That is SQLAlchemy's
    precedent and it sidesteps SOQL operator precedence entirely: the produced
    query means what the tree says regardless of how the org would have
    associated a bare expression.
    """

    __slots__ = ("left", "operator", "right")

    def __init__(self, operator: str, left: Condition, right: Condition) -> None:
        self.operator = operator
        self.left = left
        self.right = right

    def render(self) -> str:
        return f"({self.left.render()}) {self.operator} ({self.right.render()})"

    def fields(self) -> frozenset["Field[Any]"]:
        return self.left.fields() | self.right.fields()

    def __repr__(self) -> str:
        return f"Composite({self.render()!r})"


# Salesforce's documented quoted-string escape sequences, verbatim from the
# SOQL and SOSL Reference ("Quoted String Escape Sequences"). The docs are
# explicit that backslash is the escape character and that "if you use a
# backslash character in any other context, an error occurs" — so a literal
# backslash must be doubled, and it is escaped first by construction here
# because the loop below rewrites each character exactly once.
#
# Deliberately NOT in this map: \_ and \% . Those are documented as LIKE-only
# sequences that match a literal underscore or percent sign. Outside a LIKE
# pattern they are ordinary characters, and inside one they are the wildcards
# the caller asked for (D8).
_ESCAPES = {
    "\\": "\\\\",
    "'": "\\'",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",  # the docs call this "Bell"; Python's \b is backspace, 0x08
    "\f": "\\f",
}

# LIKE-only wildcards. escape_like_wildcards() applies these; render_literal
# never does.
_LIKE_WILDCARDS = {"_": "\\_", "%": "\\%"}


def escape_string(value: str) -> str:
    """Escape a string for use inside a single-quoted SOQL literal.

    The single place string escaping happens. Every character is rewritten at
    most once, so there is no order-of-replacement bug: escaping the backslash
    cannot re-escape the backslashes introduced by other sequences.

    Control characters with no documented sequence are emitted as ``\\uXXXX``,
    which the same reference documents for arbitrary code points.
    """
    out = []
    for char in value:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return "".join(out)


def escape_like_wildcards(pattern: str) -> str:
    """Escape ``_`` and ``%`` so they match literally inside a LIKE pattern.

    Not applied automatically: in a LIKE pattern those characters are the
    wildcards the caller asked for. Use this when the value is user-supplied
    data that should match literally (D8).
    """
    return "".join(_LIKE_WILDCARDS.get(char, char) for char in pattern)


def render_literal(value: object) -> str:
    """Render a Python value as a SOQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        # Before int: bool is a subclass of int.
        return "true" if value else "false"
    if isinstance(value, str):
        return f"'{escape_string(value)}'"
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
        return Filter(self, ">", other)

    def __ge__(self, other: T) -> Filter:
        return Filter(self, ">=", other)

    def __lt__(self, other: T) -> Filter:
        return Filter(self, "<", other)

    def __le__(self, other: T) -> Filter:
        return Filter(self, "<=", other)

    # __eq__ and __ne__ DO override object, which types them as
    # (object) -> bool. Returning a Filter from a narrowed parameter violates
    # Liskov, and mypy is right to say so — but the violation is the whole
    # point of the design, and it is the same trade-off SQLAlchemy makes for
    # its column expressions. Silenced deliberately, per operator, not
    # module-wide (D7).
    def __eq__(self, other: T) -> Filter:  # type: ignore[override]
        return Filter(self, "=", other)

    def __ne__(self, other: T) -> Filter:  # type: ignore[override]
        # Defined explicitly: Python's derived __ne__ negates __eq__'s result,
        # which would call Filter.__bool__ and raise. Users expect != to work.
        return Filter(self, "!=", other)

    def like(self, pattern: str) -> Filter:
        """Build a LIKE filter. ``%`` and ``_`` in ``pattern`` stay wildcards.

        Wrap the pattern in :func:`escape_like_wildcards` when it holds
        user-supplied data that should match literally (D8).
        """
        return Filter(self, "LIKE", pattern)

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
