import json

import pytest

from soqlmodel.check import Severity, exit_code, has_critical
from soqlmodel.config import Config
from soqlmodel.errors import ConfigError, SnapshotError
from soqlmodel.project import (
    MissingSnapshotError,
    check_all,
    committed_sobjects,
    generate_all,
    load_snapshot,
    orphaned_snapshots,
    snapshot_all,
    snapshot_path,
)

ORG = "FULL Sandbox"


def describe(sobject, *fields):
    return {"name": sobject, "fields": list(fields)}


def raw_field(name, **overrides):
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


ORG_SCHEMA = {
    "Account": describe(
        "Account",
        raw_field("Name"),
        raw_field("AnnualRevenue", type="currency"),
        raw_field("Website", type="url"),
    ),
    "Opportunity": describe(
        "Opportunity",
        raw_field("Name"),
        raw_field("Amount", type="currency"),
        raw_field("StageName", type="picklist", picklistValues=[{"value": "New"}]),
    ),
}


@pytest.fixture
def org(monkeypatch):
    """A fake org, patched into both the extract and check call sites."""

    def fake_fetch(sobject, org_alias):
        if sobject not in ORG_SCHEMA:
            raise KeyError(sobject)
        return ORG_SCHEMA[sobject]

    monkeypatch.setattr("soqlmodel.project.fetch_describe", fake_fetch)
    monkeypatch.setattr("soqlmodel.check.fetch_describe", fake_fetch)
    return ORG_SCHEMA


def config(**objects):
    return Config(org=ORG, objects=objects or {"Account": None, "Opportunity": None})


# --- snapshot_all -----------------------------------------------------------


def test_snapshot_all_writes_one_file_per_sobject(org, tmp_path):
    written = snapshot_all(config(), tmp_path)

    assert [path.name for path in written] == ["Account.json", "Opportunity.json"]
    assert (tmp_path / "Account.json").is_file()
    assert (tmp_path / "Opportunity.json").is_file()


def test_snapshot_all_creates_the_directory(org, tmp_path):
    target = tmp_path / "nested" / "schema"
    snapshot_all(config(), target)

    assert (target / "Account.json").is_file()


def test_snapshots_are_scoped_to_the_config(org, tmp_path):
    snapshot_all(config(Account=frozenset({"Name"})), tmp_path)
    snapshot = load_snapshot("Account", tmp_path)

    assert [f["name"] for f in snapshot["fields"]] == ["Name"]
    assert snapshot["requested_fields"] == ["Name"]


def test_snapshot_all_is_strict_about_declared_fields(org, tmp_path):
    with pytest.raises(SnapshotError, match="Account: requested field"):
        snapshot_all(config(Account=frozenset({"Name", "Nope__c"})), tmp_path)


def test_the_strict_error_names_the_object_and_field(org, tmp_path):
    with pytest.raises(SnapshotError, match="'Nope__c'"):
        snapshot_all(config(Account=frozenset({"Nope__c"})), tmp_path)


def test_snapshot_all_is_deterministic(org, tmp_path):
    snapshot_all(config(), tmp_path)
    first = (tmp_path / "Account.json").read_bytes()

    snapshot_all(config(), tmp_path)
    assert (tmp_path / "Account.json").read_bytes() == first


def test_snapshot_all_needs_an_org(org, tmp_path):
    with pytest.raises(ConfigError, match="no org configured"):
        snapshot_all(Config(objects={"Account": None}), tmp_path)


def test_snapshot_all_needs_objects(org, tmp_path):
    with pytest.raises(ConfigError, match="no sObjects configured"):
        snapshot_all(Config(org=ORG), tmp_path)


# --- generate_all -----------------------------------------------------------


def test_generate_all_writes_one_module_with_every_class(org, tmp_path):
    snapshot_all(config(), tmp_path)
    out = generate_all(config(), tmp_path, tmp_path / "models.py")

    source = out.read_text(encoding="utf-8")
    assert "class Account:" in source
    assert "class Opportunity:" in source


def test_classes_are_ordered_by_sobject_name(org, tmp_path):
    snapshot_all(config(), tmp_path)
    source = generate_all(config(), tmp_path, tmp_path / "models.py").read_text(encoding="utf-8")

    assert source.index("class Account:") < source.index("class Opportunity:")


def test_generated_module_is_byte_identical_across_runs(org, tmp_path):
    snapshot_all(config(), tmp_path)
    first = generate_all(config(), tmp_path, tmp_path / "a.py").read_bytes()
    second = generate_all(config(), tmp_path, tmp_path / "b.py").read_bytes()

    assert first == second
    assert b"\r\n" not in first


def test_ordering_does_not_depend_on_directory_order(org, tmp_path):
    # Write Opportunity first, Account second; the module must not care.
    snapshot_all(config(Opportunity=None), tmp_path)
    snapshot_all(config(Account=None), tmp_path)
    source = generate_all(config(), tmp_path, tmp_path / "models.py").read_text(encoding="utf-8")

    assert source.index("class Account:") < source.index("class Opportunity:")


def test_imports_are_the_union_across_classes(org, tmp_path):
    snapshot_all(config(), tmp_path)
    source = generate_all(config(), tmp_path, tmp_path / "models.py").read_text(encoding="utf-8")

    assert source.count("from soqlmodel.fields import Field") == 1
    assert source.count("# Generated by soqlmodel") == 1


def test_generate_all_errors_when_a_snapshot_is_missing(org, tmp_path):
    snapshot_all(config(Account=None), tmp_path)

    with pytest.raises(MissingSnapshotError, match="run snapshot first"):
        generate_all(config(), tmp_path, tmp_path / "models.py")


def test_the_missing_snapshot_error_names_the_object_and_path(org, tmp_path):
    with pytest.raises(MissingSnapshotError, match="Opportunity"):
        generate_all(config(Opportunity=None), tmp_path, tmp_path / "models.py")


def test_generate_all_still_includes_an_orphaned_snapshot(org, tmp_path):
    # The file is there and someone's code may import the class. check reports
    # it; removing it is the user's call.
    snapshot_all(config(), tmp_path)
    source = generate_all(config(Account=None), tmp_path, tmp_path / "models.py").read_text(
        encoding="utf-8"
    )

    assert "class Opportunity:" in source


def test_the_generated_module_type_checks(org, tmp_path):
    import subprocess
    import sys

    snapshot_all(config(), tmp_path)
    out = generate_all(config(), tmp_path, tmp_path / "models.py")

    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# --- both writers create their directory, through the same helper -----------


def test_generate_all_creates_a_missing_output_directory(org, tmp_path):
    snapshot_all(config(), tmp_path)

    out = generate_all(config(), tmp_path, tmp_path / "deep" / "nested" / "models.py")

    assert out.is_file()


def test_generate_all_handles_a_bare_filename(org, monkeypatch, tmp_path):
    """The default `--output models.py`, whose `.parent` is `Path(".")`.

    Pins that mkdir on it is harmless, which is what lets `_ensure_dir` carry
    no special case for it.
    """
    snapshot_all(config(), tmp_path / "schema")
    monkeypatch.chdir(tmp_path)

    assert generate_all(config(), tmp_path / "schema", "models.py").is_file()


def test_snapshot_all_creates_a_missing_schema_directory(org, tmp_path):
    written = snapshot_all(config(), tmp_path / "deep" / "nested")

    assert all(path.is_file() for path in written)


# --- orphans ----------------------------------------------------------------


def test_orphaned_snapshots_are_listed(org, tmp_path):
    snapshot_all(config(), tmp_path)

    assert orphaned_snapshots(config(Account=None), tmp_path) == ["Opportunity"]


def test_no_orphans_when_the_config_matches(org, tmp_path):
    snapshot_all(config(), tmp_path)

    assert orphaned_snapshots(config(), tmp_path) == []


def test_check_all_reports_an_orphan_without_deleting_it(org, tmp_path):
    snapshot_all(config(), tmp_path)
    path = snapshot_path("Opportunity", tmp_path)

    changes = check_all(config(Account=None), tmp_path)

    assert path.is_file(), "a committed file is not ours to delete"
    orphan = [c for c in changes if c.sobject == "Opportunity"]
    assert orphan[0].severity is Severity.WARNING
    assert "no longer declared" in orphan[0].message


def test_an_orphan_alone_does_not_fail_the_build(org, tmp_path):
    snapshot_all(config(), tmp_path)

    assert exit_code(check_all(config(Account=None), tmp_path)) == 0


def test_committed_sobjects_lists_what_is_on_disk(org, tmp_path):
    snapshot_all(config(), tmp_path)

    assert committed_sobjects(tmp_path) == ["Account", "Opportunity"]


def test_committed_sobjects_is_empty_for_a_missing_directory(tmp_path):
    assert committed_sobjects(tmp_path / "nope") == []


def test_a_snapshot_with_a_utf8_bom_is_refused(org, tmp_path):
    # A snapshot that has been through a BOM-writing editor or a Windows shell
    # redirect. Tolerating it would let `check` say "no drift" about bytes that
    # are not the ones `snapshot` writes (D13). soqlmodel.toml still tolerates
    # a BOM — it is hand-written, this is not.
    snapshot_all(config(Account=None), tmp_path)
    path = snapshot_path("Account", tmp_path)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    with pytest.raises(SnapshotError, match="UTF-8 BOM"):
        load_snapshot("Account", tmp_path)


def test_check_refuses_a_bom_rather_than_reporting_no_drift(org, tmp_path):
    """The guard's whole reason to exist: check must not call this clean."""
    snapshot_all(config(Account=None), tmp_path)
    path = snapshot_path("Account", tmp_path)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    with pytest.raises(SnapshotError, match="re-run snapshot"):
        check_all(config(Account=None), tmp_path)


def test_re_running_snapshot_heals_a_bom(org, tmp_path):
    """And the fix the error names actually works."""
    written = snapshot_all(config(Account=None), tmp_path)
    clean = written[0].read_bytes()
    written[0].write_bytes(b"\xef\xbb\xbf" + clean)

    snapshot_all(config(Account=None), tmp_path)

    assert written[0].read_bytes() == clean
    assert check_all(config(Account=None), tmp_path) == []


# --- check_all --------------------------------------------------------------


def test_check_all_is_clean_against_an_unchanged_org(org, tmp_path):
    snapshot_all(config(), tmp_path)

    assert check_all(config(), tmp_path) == []
    assert exit_code(check_all(config(), tmp_path)) == 0


def test_check_all_errors_when_a_snapshot_is_missing(org, tmp_path):
    with pytest.raises(MissingSnapshotError, match="run snapshot first"):
        check_all(config(), tmp_path)


def test_check_all_aggregates_criticals_across_objects(org, tmp_path):
    snapshot_all(config(), tmp_path)

    # Doctor both committed snapshots so each object drifts.
    for sobject, field_name in (("Account", "Name"), ("Opportunity", "Amount")):
        path = snapshot_path(sobject, tmp_path)
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        for field in snapshot["fields"]:
            if field["name"] == field_name:
                field["type"] = "somethingElse"
        path.write_text(json.dumps(snapshot), encoding="utf-8")

    changes = check_all(config(), tmp_path)

    assert {c.sobject for c in changes} == {"Account", "Opportunity"}
    assert all(c.severity is Severity.CRITICAL for c in changes)
    assert exit_code(changes) == 1


def test_one_critical_anywhere_fails_the_build(org, tmp_path):
    snapshot_all(config(), tmp_path)

    path = snapshot_path("Opportunity", tmp_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["fields"].append({"name": "Vanished__c", "type": "string"})
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    changes = check_all(config(), tmp_path)

    assert has_critical(changes)
    assert exit_code(changes) == 1
    # Account is clean; the failure comes from Opportunity alone.
    assert [c.sobject for c in changes] == ["Opportunity"]


def test_check_all_sorts_criticals_before_warnings(org, tmp_path):
    snapshot_all(config(), tmp_path)

    account = snapshot_path("Account", tmp_path)
    snapshot = json.loads(account.read_text(encoding="utf-8"))
    for field in snapshot["fields"]:
        if field["name"] == "Name":
            field["length"] = 1  # WARNING
        if field["name"] == "Website":
            field["type"] = "int"  # CRITICAL
    account.write_text(json.dumps(snapshot), encoding="utf-8")

    changes = check_all(config(), tmp_path)

    assert changes[0].severity is Severity.CRITICAL
    assert changes[-1].severity is Severity.WARNING


def test_check_all_does_not_crash_on_a_deleted_declared_field(org, tmp_path):
    # strict=False on the live side (D9): a CRITICAL line, not a traceback.
    snapshot_all(config(Account=frozenset({"Name"})), tmp_path)

    path = snapshot_path("Account", tmp_path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["requested_fields"] = ["Deleted__c", "Name"]
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    scoped = Config(org=ORG, objects={"Account": frozenset({"Name", "Deleted__c"})})
    changes = check_all(scoped, tmp_path)

    assert [c.field for c in changes] == ["Deleted__c"]
    assert changes[0].severity is Severity.CRITICAL


# --- round trip -------------------------------------------------------------


def test_snapshot_generate_check_round_trip(org, tmp_path):
    conf = config()

    snapshot_all(conf, tmp_path)
    module = generate_all(conf, tmp_path, tmp_path / "models.py")
    changes = check_all(conf, tmp_path)

    source = module.read_text(encoding="utf-8")
    assert "class Account:" in source and "class Opportunity:" in source
    assert changes == []
    assert exit_code(changes) == 0
