"""Trim a Salesforce describe payload into a stable, diffable snapshot.

Salesforce returns 57 properties per field. We keep the 16 that matter for
drift detection (see DECISIONS.md, D3) and drop the rest, so a diff of two
snapshots reads as a list of real schema changes.

Pure functions only — nothing here touches the network. The caller supplies
an already-extracted describe dict.
"""

from typing import Any

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

    Properties are kept when present, regardless of value — ``nillable: false``
    survives, it is not treated as missing. ``picklistValues`` is flattened from
    a list of objects to a sorted list of the *active* value strings (D4), and is
    omitted entirely for fields left with no active values.
    """
    trimmed = {prop: field[prop] for prop in _KEPT_PROPERTIES if prop in field}

    picklist_values = sorted(
        entry["value"]
        for entry in field.get("picklistValues") or ()
        # A missing "active" key means active — some payloads omit it.
        if "value" in entry and entry.get("active", True)
    )
    if picklist_values:
        trimmed["picklistValues"] = picklist_values

    return trimmed


def build_snapshot(describe: dict[str, Any], org: str) -> dict[str, Any]:
    """Build the snapshot for one sobject describe payload.

    Fields are sorted by name and each is trimmed. Nothing time-varying is
    recorded: re-running this against an unchanged org yields an identical
    snapshot.

    Raises:
        ValueError: if the payload has no ``name`` key, which means it is not a
            describe result (most often the ``sf`` CLI envelope was passed in
            instead of its ``result`` value).
    """
    if "name" not in describe:
        raise ValueError("describe payload has no 'name' key; not an sobject describe result")

    fields = [trim_field(field) for field in describe.get("fields") or ()]
    fields.sort(key=lambda field: field.get("name", ""))

    return {
        "org": org,
        "sobject": describe["name"],
        "fields": fields,
    }
