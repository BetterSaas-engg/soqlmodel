import copy
import json

import pytest

from soqlmodel.check import (
    Change,
    Severity,
    check,
    diff_snapshots,
    exit_code,
    format_report,
    has_critical,
)
from soqlmodel.config import Config


def field(name, **overrides):
    base = {
        "name": name,
        "label": name,
        "type": "string",
        "nillable": True,
        "filterable": True,
        "sortable": True,
        "custom": False,
        "calculated": False,
        "length": 255,
        "precision": 0,
        "scale": 0,
        "restrictedPicklist": False,
        "deprecatedAndHidden": False,
    }
    base.update(overrides)
    return base


def snapshot(*fields, sobject="Account", org="Prod", requested=None):
    snap = {
        "format_version": 1,
        "org": org,
        "sobject": sobject,
        "fields": list(fields),
    }
    if requested is not None:
        snap["requested_fields"] = sorted(requested)
    return snap


def messages(changes):
    return [c.message for c in changes]


def severities(changes):
    return {c.severity for c in changes}


# --- no drift ---------------------------------------------------------------


def test_identical_snapshots_produce_no_changes():
    snap = snapshot(field("Name"), field("Industry", type="picklist"))

    assert diff_snapshots(snap, copy.deepcopy(snap)) == []


def test_no_drift_reports_cleanly():
    assert format_report([]) == "No drift."


def test_no_changes_exits_zero():
    assert exit_code([]) == 0


# --- format_version short-circuit -------------------------------------------


def test_a_format_mismatch_short_circuits():
    committed = snapshot(field("Name"))
    live = snapshot(field("Gone"), field("Different", type="int"))
    live["format_version"] = 2

    changes = diff_snapshots(committed, live)

    assert len(changes) == 1
    assert "snapshot format changed from 1 to 2" in changes[0].message


def test_the_format_message_says_to_regenerate():
    committed = snapshot(field("Name"))
    live = snapshot(field("Name"))
    live["format_version"] = 7

    assert "regenerate" in diff_snapshots(committed, live)[0].message


def test_the_format_message_blames_the_format_not_the_org():
    committed = snapshot(field("Name"))
    live = snapshot(field("Name"))
    live["format_version"] = 2

    assert "not the org" in diff_snapshots(committed, live)[0].message


def test_a_format_mismatch_hides_real_drift():
    # Deliberate: diffing across formats would report our reformatting as
    # drift, which is the false positive D6 exists to prevent.
    committed = snapshot(field("Name", type="string"))
    live = snapshot(field("Name", type="int"))
    live["format_version"] = 2

    assert messages(diff_snapshots(committed, live)) != ["type changed..."]
    assert len(diff_snapshots(committed, live)) == 1


def test_a_format_mismatch_is_critical():
    committed = snapshot(field("Name"))
    live = snapshot(field("Name"))
    live["format_version"] = 2

    assert has_critical(diff_snapshots(committed, live))


# --- CRITICAL ---------------------------------------------------------------


def test_a_removed_field_is_critical():
    changes = diff_snapshots(snapshot(field("Name"), field("Gone__c")), snapshot(field("Name")))

    assert changes[0].severity is Severity.CRITICAL
    assert changes[0].field == "Gone__c"
    assert changes[0].message == "field no longer exists in the org"


def test_a_declared_field_absent_from_the_org_is_critical():
    # The scoped case: the declaration outlives the field, so the live
    # snapshot (built with strict=False) carries the request but not the field.
    committed = snapshot(field("Name"), requested=["Name", "Contract_End__c"])
    live = snapshot(field("Name"), requested=["Name", "Contract_End__c"])

    changes = diff_snapshots(committed, live)

    assert [c.field for c in changes] == ["Contract_End__c"]
    assert changes[0].severity is Severity.CRITICAL


def test_a_type_change_is_critical():
    changes = diff_snapshots(
        snapshot(field("Amount", type="double")), snapshot(field("Amount", type="string"))
    )

    assert changes[0].severity is Severity.CRITICAL
    assert changes[0].message == "type changed from 'double' to 'string'"


def test_a_removed_picklist_value_is_critical():
    committed = snapshot(field("StageName", type="picklist", picklistValues=["Closed Won", "New"]))
    live = snapshot(field("StageName", type="picklist", picklistValues=["New"]))

    changes = diff_snapshots(committed, live)

    assert changes[0].severity is Severity.CRITICAL
    assert changes[0].message == 'value removed "Closed Won"'


def test_every_removed_value_gets_its_own_line():
    committed = snapshot(field("S", type="picklist", picklistValues=["A", "B", "C"]))
    live = snapshot(field("S", type="picklist", picklistValues=["C"]))

    assert messages(diff_snapshots(committed, live)) == ['value removed "A"', 'value removed "B"']


def test_filterable_becoming_false_is_critical():
    changes = diff_snapshots(
        snapshot(field("Name", filterable=True)), snapshot(field("Name", filterable=False))
    )

    assert changes[0].severity is Severity.CRITICAL
    assert "no longer filterable" in changes[0].message


def test_sortable_becoming_false_is_critical():
    changes = diff_snapshots(
        snapshot(field("Name", sortable=True)), snapshot(field("Name", sortable=False))
    )

    assert changes[0].severity is Severity.CRITICAL
    assert "no longer sortable" in changes[0].message


def test_becoming_deprecated_and_hidden_is_critical():
    changes = diff_snapshots(
        snapshot(field("Old__c", deprecatedAndHidden=False)),
        snapshot(field("Old__c", deprecatedAndHidden=True)),
    )

    assert changes[0].severity is Severity.CRITICAL
    assert changes[0].message == "deprecated and hidden"


def test_filterable_becoming_true_is_not_reported():
    # Gaining a capability breaks nothing.
    changes = diff_snapshots(
        snapshot(field("Name", filterable=False)), snapshot(field("Name", filterable=True))
    )

    assert changes == []


# --- WARNING ----------------------------------------------------------------


def test_an_added_picklist_value_is_a_warning():
    committed = snapshot(field("Industry", type="picklist", picklistValues=["Tech"]))
    live = snapshot(field("Industry", type="picklist", picklistValues=["Renewables", "Tech"]))

    changes = diff_snapshots(committed, live)

    assert changes[0].severity is Severity.WARNING
    assert changes[0].message == 'value added "Renewables"'


def test_a_new_field_is_a_warning():
    changes = diff_snapshots(snapshot(field("Name")), snapshot(field("Name"), field("New__c")))

    assert changes[0].severity is Severity.WARNING
    assert changes[0].field == "New__c"
    assert changes[0].message == "new field appeared"


def test_nillable_changing_is_a_warning():
    changes = diff_snapshots(
        snapshot(field("Name", nillable=True)), snapshot(field("Name", nillable=False))
    )

    assert changes[0].severity is Severity.WARNING
    assert changes[0].message == "nillable changed from True to False"


@pytest.mark.parametrize("prop", ["length", "precision", "scale"])
def test_size_properties_changing_are_warnings(prop):
    changes = diff_snapshots(
        snapshot(field("Name", **{prop: 10})), snapshot(field("Name", **{prop: 20}))
    )

    assert changes[0].severity is Severity.WARNING
    assert changes[0].message == f"{prop} changed from 10 to 20"


# --- ordering and reporting -------------------------------------------------


def test_criticals_sort_before_warnings():
    committed = snapshot(field("Name"), field("Amount", type="double"))
    live = snapshot(field("Name", nillable=False), field("Amount", type="string"))

    changes = diff_snapshots(committed, live)

    assert changes[0].severity is Severity.CRITICAL
    assert changes[-1].severity is Severity.WARNING


def test_the_report_looks_like_the_spec():
    changes = [
        Change(Severity.CRITICAL, "Opportunity", "StageName", 'value removed "Closed Won"'),
        Change(Severity.WARNING, "Account", "Industry", 'value added "Renewables"'),
    ]

    assert format_report(changes) == (
        'CRITICAL  Opportunity.StageName: value removed "Closed Won"\n'
        'WARNING   Account.Industry: value added "Renewables"'
    )


def test_the_diff_is_deterministic():
    committed = snapshot(field("B"), field("A", type="int"), field("C"))
    live = snapshot(field("B", nillable=False), field("A", type="string"), field("D"))

    assert diff_snapshots(committed, live) == diff_snapshots(committed, live)


def test_an_object_level_change_renders_without_a_field():
    change = Change(Severity.CRITICAL, "Account", "", "snapshot format changed")

    assert change.render() == "CRITICAL  Account: snapshot format changed"


# --- exit codes -------------------------------------------------------------


def test_a_critical_exits_one():
    changes = [Change(Severity.CRITICAL, "Account", "Name", "gone")]

    assert exit_code(changes) == 1


def test_warnings_alone_exit_zero():
    # Documented choice: warnings inform, they do not block. A check that
    # blocks on things nobody can act on is a check people mute (D10).
    changes = [Change(Severity.WARNING, "Account", "Industry", 'value added "X"')]

    assert exit_code(changes) == 0
    assert not has_critical(changes)


def test_a_critical_among_warnings_still_exits_one():
    changes = [
        Change(Severity.WARNING, "Account", "Industry", 'value added "X"'),
        Change(Severity.CRITICAL, "Account", "Name", "gone"),
    ]

    assert exit_code(changes) == 1


# --- the thin network caller ------------------------------------------------


def test_check_reads_the_snapshot_and_diffs_what_extract_returns(tmp_path, monkeypatch):
    committed = snapshot(field("Name"), field("Gone__c"), org="FULL Sandbox")
    path = tmp_path / "account.schema.json"
    path.write_text(json.dumps(committed), encoding="utf-8")

    captured = {}

    def fake_fetch(sobject, org):
        captured["sobject"] = sobject
        captured["org"] = org
        return {"name": "Account", "fields": [field("Name")]}

    monkeypatch.setattr("soqlmodel.check.fetch_describe", fake_fetch)

    changes = check(Config(org="FULL Sandbox"), path)

    assert captured == {"sobject": "Account", "org": "FULL Sandbox"}
    assert [c.field for c in changes] == ["Gone__c"]
    assert changes[0].severity is Severity.CRITICAL


def test_check_does_not_raise_when_a_declared_field_vanished(tmp_path, monkeypatch):
    # The whole point of strict=False here: a CRITICAL line, not a traceback.
    committed = snapshot(field("Name"), requested=["Name", "Contract_End__c"])
    path = tmp_path / "account.schema.json"
    path.write_text(json.dumps(committed), encoding="utf-8")

    monkeypatch.setattr(
        "soqlmodel.check.fetch_describe",
        lambda sobject, org: {"name": "Account", "fields": [field("Name")]},
    )

    config = Config(org="Prod", objects={"Account": frozenset({"Name", "Contract_End__c"})})
    changes = check(config, path)

    assert [c.field for c in changes] == ["Contract_End__c"]
    assert exit_code(changes) == 1


def test_check_falls_back_to_the_snapshots_org(tmp_path, monkeypatch):
    committed = snapshot(field("Name"), org="eyeo-qa")
    path = tmp_path / "account.schema.json"
    path.write_text(json.dumps(committed), encoding="utf-8")

    captured = {}

    def fake_fetch(sobject, org):
        captured["org"] = org
        return {"name": "Account", "fields": [field("Name")]}

    monkeypatch.setattr("soqlmodel.check.fetch_describe", fake_fetch)
    check(Config(), path)

    assert captured["org"] == "eyeo-qa"
