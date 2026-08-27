"""Trim a Salesforce describe payload into a stable, diffable snapshot.

Salesforce returns 57 properties per field. We keep the 16 that matter for
drift detection (see DECISIONS.md, D3) and drop the rest, so a diff of two
snapshots reads as a list of real schema changes.

Pure functions only — nothing here touches the network. The caller supplies
an already-extracted describe dict.
"""

from collections.abc import Iterable
from typing import Any

from soqlmodel.errors import SnapshotError

# Bumped whenever a change to what we store alters the bytes of an existing
# snapshot. Lets `check` tell "the format moved, regenerate" apart from
# "the org's schema drifted" (D6).
SNAPSHOT_FORMAT_VERSION = 1

# Kept properties, in the order they appear in a trimmed field. Order is fixed
# so trimmed dicts are byte-identical across runs, not just equal.
_KEPT_PROPERTIES = (
    "name",
    "label",
    "type",
    "nillable",
    "filterable",
    "sortable",
    "referenceTo",
    "relationshipName",
    "deprecatedAndHidden",
    "custom",
    "calculated",
    "length",
    "precision",
    "scale",
    "restrictedPicklist",
)


def trim_field(field: dict[str, Any]) -> dict[str, Any]:
    """Reduce one describe field to the properties worth tracking.

    Properties are kept when present and non-null. Falsey-but-not-null values
    survive — ``nillable: false`` and ``length: 0`` are real answers, not
    missing ones — while ``null`` is dropped as noise (D5). ``picklistValues``
    is flattened from a list of objects to a sorted list of the *active* value
    strings (D4), and is omitted for fields left with no active values.

    Raises:
        SnapshotError: if the field has no usable ``name``. That is a corrupt
            payload, not an edge case — better to fail here than to emit a
            nameless field for the generator to choke on later.
    """
    if field.get("name") is None:
        raise SnapshotError(f"describe field has no 'name': {field!r}")

    trimmed = {
        prop: field[prop] for prop in _KEPT_PROPERTIES if prop in field and field[prop] is not None
    }

    picklist_values = sorted(
        entry["value"]
        for entry in field.get("picklistValues") or ()
        # A missing "active" key means active — some payloads omit it.
        if "value" in entry and entry.get("active", True)
    )
    if picklist_values:
        trimmed["picklistValues"] = picklist_values

    return trimmed


def _apply_scope(
    fields: list[dict[str, Any]], requested: Iterable[str], sobject: str, strict: bool
) -> list[dict[str, Any]]:
    """Keep only requested fields, reporting ones the org does not have.

    Under ``strict`` a requested field that does not exist raises: the caller
    is declaring a dependency, and silently returning a smaller model would
    produce code that compiles and a pipeline that reads nothing. Without it
    the field is simply absent from the result, for callers asking *what
    changed* rather than declaring what they need (D9).
    """
    by_name = {field["name"]: field for field in fields}
    kept = [by_name[name] for name in requested if name in by_name]

    if not strict:
        return kept

    lowered = {name.lower(): name for name in by_name}
    missing = []
    for name in requested:
        if name in by_name:
            continue
        suggestion = lowered.get(name.lower())
        missing.append(f"{name!r}{f' (did you mean {suggestion!r}?)' if suggestion else ''}")

    if missing:
        raise SnapshotError(
            f"{sobject}: requested field(s) not present in the org: {', '.join(sorted(missing))}"
        )

    return kept


def missing_fields(snapshot: dict[str, Any]) -> list[str]:
    """Declared fields the org did not return, sorted.

    Empty for an unscoped snapshot — nothing was declared, so nothing can be
    missing. This is how `check` reads the result of a non-strict rebuild:
    a declared field that has disappeared is the drift that matters most, and
    it deserves a CRITICAL line rather than a stack trace (D9).
    """
    requested = snapshot.get("requested_fields")
    if requested is None:
        return []

    present = {field["name"] for field in snapshot.get("fields") or ()}
    return sorted(set(requested) - present)


def build_snapshot(
    describe: dict[str, Any],
    org: str,
    fields: Iterable[str] | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Build the snapshot for one sobject describe payload.

    Fields are sorted by name and each is trimmed. Nothing time-varying is
    recorded: re-running this against an unchanged org yields an identical
    snapshot.

    Args:
        describe: one sobject describe payload, already unwrapped.
        org: the org alias, recorded in the snapshot.
        fields: the field names this project depends on. ``None`` means every
            field — the unscoped default. When given, the requested names are
            recorded in the snapshot so `check` can tell "a field I asked for
            is gone" from "a field I never asked for" (D9).
        strict: raise if a requested field is not on the sobject. True for
            snapshot and generate, which are declaring a dependency. `check`
            passes False and reads :func:`missing_fields` instead — asking
            what changed must not crash on the answer.

    Raises:
        SnapshotError: if the payload has no ``name`` key, which means it is not a
            describe result (most often the ``sf`` CLI envelope was passed in
            instead of its ``result`` value); or, under ``strict``, if a
            requested field does not exist on the sobject.
    """
    if "name" not in describe:
        raise SnapshotError("describe payload has no 'name' key; not an sobject describe result")

    trimmed = [trim_field(field) for field in describe.get("fields") or ()]

    requested = None if fields is None else sorted(set(fields))
    if requested is not None:
        trimmed = _apply_scope(trimmed, requested, describe["name"], strict=strict)

    # Case-insensitive so lowercase custom fields (eCPM__c, ssp_link__c) sit
    # with their siblings rather than in a clump at the bottom; the exact name
    # breaks ties so the order stays total and deterministic (D5).
    trimmed.sort(key=lambda field: (field["name"].lower(), field["name"]))

    snapshot: dict[str, Any] = {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "org": org,
        "sobject": describe["name"],
        "fields": trimmed,
    }

    if requested is not None:
        # Recorded only when scoped, so unscoped snapshots keep the bytes they
        # had before scoping existed (D9).
        snapshot["requested_fields"] = requested

    return snapshot
