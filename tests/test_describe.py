import json

import pytest

from soqlmodel.describe import build_snapshot, trim_field


def make_field(name: str, **overrides: object) -> dict:
    """A describe field carrying both kept and dropped properties."""
    field = {
        "name": name,
        "label": name,
        "type": "string",
        "nillable": True,
        "filterable": True,
        "sortable": True,
        "referenceTo": [],
        "relationshipName": None,
        "deprecatedAndHidden": False,
        "custom": False,
        "calculated": False,
        "length": 255,
        "precision": 0,
        "scale": 0,
        "restrictedPicklist": False,
        # Properties we deliberately do not track.
        "createable": True,
        "updateable": True,
        "defaultedOnCreate": False,
        "digits": 0,
        "byteLength": 765,
        "soapType": "xsd:string",
    }
    field.update(overrides)
    return field


def test_drops_untracked_properties():
    # relationshipName is absent because the fixture leaves it null.
    trimmed = trim_field(make_field("Name"))

    assert set(trimmed) == {
        "name",
        "label",
        "type",
        "nillable",
        "filterable",
        "sortable",
        "referenceTo",
        "deprecatedAndHidden",
        "custom",
        "calculated",
        "length",
        "precision",
        "scale",
        "restrictedPicklist",
    }


def test_keeps_falsey_values():
    # Falsey is a real answer; only null is noise. A required field must not
    # lose its nillable: false to the None-dropping rule.
    trimmed = trim_field(make_field("Name", nillable=False, length=0))

    assert trimmed["nillable"] is False
    assert trimmed["length"] == 0


def test_drops_null_valued_properties():
    trimmed = trim_field(make_field("Name", relationshipName=None, scale=None))

    assert "relationshipName" not in trimmed
    assert "scale" not in trimmed


def test_keeps_empty_containers():
    # An empty list is not null — referenceTo: [] is kept.
    assert trim_field(make_field("Name", referenceTo=[]))["referenceTo"] == []


def test_a_field_gaining_a_relationship_shows_as_an_added_key():
    without = trim_field(make_field("OwnerId", relationshipName=None))
    with_rel = trim_field(make_field("OwnerId", relationshipName="Owner"))

    assert set(with_rel) - set(without) == {"relationshipName"}


def test_keeps_only_properties_that_are_present():
    trimmed = trim_field({"name": "Id", "type": "id", "byteLength": 18})

    assert trimmed == {"name": "Id", "type": "id"}


def make_picklist_field(*values: dict) -> dict:
    return make_field("Type", type="picklist", picklistValues=list(values))


def test_flattens_and_sorts_picklist_values():
    field = make_picklist_field(
        {"value": "Prospect", "label": "Prospect", "active": True, "defaultValue": False},
        {"value": "Customer", "label": "Customer", "active": True, "defaultValue": False},
    )

    assert trim_field(field)["picklistValues"] == ["Customer", "Prospect"]


def test_excludes_inactive_picklist_values():
    # A deactivated value must drop out of the snapshot, so the diff shows a
    # removal rather than nothing at all.
    field = make_picklist_field(
        {"value": "Prospect", "active": True},
        {"value": "Analyst", "active": False},
    )

    assert trim_field(field)["picklistValues"] == ["Prospect"]


def test_treats_missing_active_key_as_active():
    field = make_picklist_field({"value": "Prospect"}, {"value": "Analyst", "active": False})

    assert trim_field(field)["picklistValues"] == ["Prospect"]


def test_omits_picklist_key_when_all_values_are_inactive():
    field = make_picklist_field(
        {"value": "Prospect", "active": False},
        {"value": "Analyst", "active": False},
    )

    assert "picklistValues" not in trim_field(field)


def test_omits_picklist_key_when_field_has_no_values():
    assert "picklistValues" not in trim_field(make_field("Name"))
    assert "picklistValues" not in trim_field(make_field("Name", picklistValues=[]))


def test_sorts_fields_by_name():
    describe = {
        "name": "Account",
        "fields": [make_field("Website"), make_field("Id"), make_field("Name")],
    }

    snapshot = build_snapshot(describe, org="Prod")

    assert [field["name"] for field in snapshot["fields"]] == ["Id", "Name", "Website"]


def test_sorts_case_insensitively():
    # A lowercase custom field belongs with its siblings, not after every
    # capitalized name as raw ASCII ordering would put it.
    describe = {
        "name": "Account",
        "fields": [make_field("Website"), make_field("eCPM_EUR__c"), make_field("Id")],
    }

    snapshot = build_snapshot(describe, org="Prod")

    assert [field["name"] for field in snapshot["fields"]] == ["eCPM_EUR__c", "Id", "Website"]


def test_case_only_collisions_have_a_deterministic_order():
    # Names differing only by case must not depend on input order.
    fields = [make_field("type__c"), make_field("Type__c")]
    forwards = build_snapshot({"name": "Account", "fields": fields}, org="Prod")
    backwards = build_snapshot({"name": "Account", "fields": fields[::-1]}, org="Prod")

    assert [f["name"] for f in forwards["fields"]] == ["Type__c", "type__c"]
    assert forwards == backwards


def test_snapshot_shape():
    describe = {"name": "Account", "fields": [make_field("Id")]}

    snapshot = build_snapshot(describe, org="Prod")

    assert snapshot["org"] == "Prod"
    assert snapshot["sobject"] == "Account"
    assert len(snapshot["fields"]) == 1


def test_snapshot_carries_a_format_version_first():
    snapshot = build_snapshot({"name": "Account"}, org="Prod")

    assert snapshot["format_version"] == 1
    assert next(iter(snapshot)) == "format_version"


def test_trim_field_rejects_a_field_with_no_name():
    with pytest.raises(ValueError, match="no 'name'"):
        trim_field({"type": "string", "label": "Nameless"})


def test_trim_field_rejects_a_null_name():
    # Null would be dropped by the None-rule, leaving a nameless field.
    field = make_field("Name")
    field["name"] = None

    with pytest.raises(ValueError, match="no 'name'"):
        trim_field(field)


def test_nameless_field_error_names_the_offending_field():
    with pytest.raises(ValueError, match="Nameless"):
        trim_field({"type": "string", "label": "Nameless"})


def test_build_snapshot_rejects_a_nameless_field():
    describe = {"name": "Account", "fields": [make_field("Id"), {"type": "string"}]}

    with pytest.raises(ValueError, match="no 'name'"):
        build_snapshot(describe, org="Prod")


def test_handles_describe_with_no_fields():
    assert build_snapshot({"name": "Account"}, org="Prod")["fields"] == []


def test_raises_on_payload_without_name():
    # The sf CLI envelope is the realistic mistake: describe lives under "result".
    with pytest.raises(ValueError):
        build_snapshot({"status": 0, "result": {"name": "Account"}}, org="Prod")


def test_serialization_is_byte_identical_regardless_of_input_order():
    fields = [make_field("Website"), make_field("Id"), make_field("Name")]
    forwards = build_snapshot({"name": "Account", "fields": fields}, org="Prod")
    backwards = build_snapshot({"name": "Account", "fields": list(reversed(fields))}, org="Prod")

    assert json.dumps(forwards) == json.dumps(backwards)
