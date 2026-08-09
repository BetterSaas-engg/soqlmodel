"""Trim a Salesforce describe payload into a stable, diffable snapshot.

Salesforce returns 57 properties per field. We keep the 16 that matter for
drift detection (see DECISIONS.md, D3) and drop the rest, so a diff of two
snapshots reads as a list of real schema changes.

Pure functions only — nothing here touches the network. The caller supplies
an already-extracted describe dict.
"""

from typing import Any

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
        ValueError: if the field has no usable ``name``. That is a corrupt
            payload, not an edge case — better to fail here than to emit a
            nameless field for the generator to choke on later.
    """
    if field.get("name") is None:
        raise ValueError(f"describe field has no 'name': {field!r}")

    trimmed = {
        prop: field[prop]
        for prop in _KEPT_PROPERTIES
        if prop in field and field[prop] is not None
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
    # Case-insensitive so lowercase custom fields (eCPM__c, ssp_link__c) sit
    # with their siblings rather than in a clump at the bottom; the exact name
    # breaks ties so the order stays total and deterministic (D5).
    fields.sort(key=lambda field: (field["name"].lower(), field["name"]))

    return {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "org": org,
        "sobject": describe["name"],
        "fields": fields,
    }
