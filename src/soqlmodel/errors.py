"""The exceptions soqlmodel raises for failures a user can cause.

Two kinds of failure live in this codebase and they are not the same kind.

A **user failure** is a config that is wrong, an org that cannot be reached, a
declared field the org does not have. It is expected, it is one line, and the
CLI turns it into ``soqlmodel: <message>`` and exit 2. Every class here is that
kind, and every class here inherits :class:`SoqlModelError`.

A **programming failure** is a caller misusing the library: ordering by a
``Field`` belonging to another sObject, comparing against a naive datetime,
``limit(0)``. Those keep raising plain ``ValueError`` and ``TypeError``, and the
CLI deliberately does not catch them. A traceback is the correct output for a
bug; converting one into a tidy message would turn a defect into a plausible
answer, which is the failure mode this project exists to prevent (D11).

That is why nothing here subclasses ``ValueError``. The whole point of the
split is that ``except SoqlModelError`` cannot accidentally swallow a bug, and
inheriting from ``ValueError`` would hand that property straight back.

One class per stage, so the class alone says where it went wrong.
"""


class SoqlModelError(Exception):
    """Base for every failure a user can cause and can fix.

    Catch this to catch everything soqlmodel raises on purpose — and nothing
    it raises by accident.
    """


class ConfigError(SoqlModelError):
    """``soqlmodel.toml`` is absent, malformed, or declares too little."""


class SfCliError(SoqlModelError):
    """The ``sf`` CLI could not be run, or reported a failure."""


class SnapshotError(SoqlModelError):
    """A describe payload or snapshot file could not be read as a snapshot."""


class GenerateError(SoqlModelError):
    """A snapshot could not be rendered as a Python module."""


class ExecuteError(SoqlModelError):
    """A query could not be handed to a client, or the cursor broke mid-drain.

    Ours only. An exception raised by the client itself — a bad SOQL string, an
    expired session, a network failure — propagates untouched: it is their
    error, they word it better than we could, and swallowing it into this class
    would lose the type a caller wants to catch on (D15).
    """
