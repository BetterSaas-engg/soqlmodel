import json
import subprocess
from unittest.mock import patch

import pytest

from soqlmodel.describe import build_snapshot
from soqlmodel.errors import CredentialError, SfCliError, SnapshotError, SoqlModelError
from soqlmodel.extract import (
    CREDENTIAL_ENV_VARS,
    SOURCE_CREDENTIALS,
    SOURCE_SF,
    SOURCES,
    extract_describe,
    fetch_describe,
    fetch_describe_via_credentials,
    read_snapshot,
    require_credentials,
    unwrap_describe,
    write_snapshot,
)


def test_unwraps_the_cli_envelope():
    payload = {"status": 0, "result": {"name": "Account", "fields": []}, "warnings": []}

    assert unwrap_describe(payload) == {"name": "Account", "fields": []}


def test_returns_already_unwrapped_payload_as_is():
    payload = {"name": "Account", "fields": []}

    assert unwrap_describe(payload) is payload


def test_raises_on_non_zero_status():
    payload = {"status": 1, "name": "NoOrgFound", "message": "No org found for alias Prod"}

    with pytest.raises(SfCliError, match="No org found for alias Prod"):
        unwrap_describe(payload)


def test_raises_on_non_zero_status_even_when_a_result_is_present():
    payload = {"status": 1, "message": "partial failure", "result": {"name": "Account"}}

    with pytest.raises(SfCliError):
        unwrap_describe(payload)


def test_error_message_survives_a_payload_with_no_message():
    with pytest.raises(SfCliError, match="status 68"):
        unwrap_describe({"status": 68})


def test_builds_the_command_as_a_list_with_the_org_alias_intact():
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"status": 0, "result": {"name": "Account"}}', stderr=""
    )

    with (
        patch("soqlmodel.extract.shutil.which", return_value="/usr/bin/sf"),
        patch("soqlmodel.extract.subprocess.run", return_value=completed) as run,
    ):
        result = fetch_describe("Account", "FULL Sandbox", "68.0")

    command = run.call_args.args[0]
    assert command == [
        "/usr/bin/sf",
        "sobject",
        "describe",
        "--sobject",
        "Account",
        "--target-org",
        # One argv entry: a shell would have split this into two.
        "FULL Sandbox",
        # Pinned, never left to the CLI to negotiate (D21).
        "--api-version",
        "68.0",
        "--json",
    ]
    assert result == {"name": "Account"}


def test_write_snapshot_is_byte_identical_across_writes(tmp_path):
    snapshot = {
        "org": "Prod",
        "sobject": "Account",
        "fields": [{"name": "Id", "type": "id"}, {"name": "Name", "type": "string"}],
    }

    first = write_snapshot(snapshot, tmp_path / "first.json").read_bytes()
    second = write_snapshot(snapshot, tmp_path / "second.json").read_bytes()

    assert first == second


def test_write_snapshot_key_order_does_not_affect_bytes(tmp_path):
    ordered = {"org": "Prod", "sobject": "Account", "fields": []}
    shuffled = {"fields": [], "sobject": "Account", "org": "Prod"}

    assert (
        write_snapshot(ordered, tmp_path / "a.json").read_bytes()
        == write_snapshot(shuffled, tmp_path / "b.json").read_bytes()
    )


def test_write_snapshot_uses_lf_and_a_trailing_newline(tmp_path):
    raw = write_snapshot({"org": "Prod"}, tmp_path / "snapshot.json").read_bytes()

    assert raw.endswith(b"\n")
    assert b"\r\n" not in raw


def test_write_snapshot_writes_non_ascii_literally(tmp_path):
    # Real labels carry non-ASCII. It should read as itself in a diff, not as
    # a \uXXXX escape — which needs ensure_ascii=False *and* an explicit utf-8
    # encoding, or this dies on a non-UTF-8 Windows codepage.
    snapshot = {"org": "Prod", "fields": [{"name": "Größe__c", "label": "Größe — 日本"}]}
    path = write_snapshot(snapshot, tmp_path / "snapshot.json")

    raw = path.read_bytes()
    assert "Größe — 日本".encode() in raw
    assert rb"\u" not in raw
    assert json.loads(path.read_text(encoding="utf-8")) == snapshot


def test_write_snapshot_round_trips(tmp_path):
    snapshot = {"org": "Prod", "sobject": "Account", "fields": [{"name": "Id"}]}
    path = write_snapshot(snapshot, tmp_path / "snapshot.json")

    assert json.loads(path.read_text(encoding="utf-8")) == snapshot


# --- read_snapshot ----------------------------------------------------------


def test_read_snapshot_round_trips_what_write_snapshot_wrote(tmp_path):
    snapshot = {"org": "Prod", "sobject": "Account", "fields": [{"name": "Größe__c"}]}
    path = write_snapshot(snapshot, tmp_path / "snapshot.json")

    assert read_snapshot(path) == snapshot


def test_read_snapshot_refuses_a_utf8_bom(tmp_path):
    # The guard proved to fail: the same bytes minus the BOM read fine below,
    # so it is the BOM being rejected and not the content.
    path = write_snapshot({"org": "Prod"}, tmp_path / "snapshot.json")
    clean = path.read_bytes()
    path.write_bytes(b"\xef\xbb\xbf" + clean)

    with pytest.raises(SnapshotError, match="UTF-8 BOM"):
        read_snapshot(path)

    path.write_bytes(clean)
    assert read_snapshot(path) == {"org": "Prod"}


def test_read_snapshot_names_the_fix_for_a_bom(tmp_path):
    path = write_snapshot({"org": "Prod"}, tmp_path / "snapshot.json")
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    with pytest.raises(SnapshotError, match="re-run snapshot to rewrite it"):
        read_snapshot(path)


def test_read_snapshot_keeps_a_bom_that_is_not_at_the_start(tmp_path):
    # A U+FEFF inside a label is data, not a byte-order mark. Rejecting the
    # file for it would be the guard overreaching.
    snapshot = {"org": "Prod", "fields": [{"label": "a" + chr(0xFEFF) + "b"}]}
    path = write_snapshot(snapshot, tmp_path / "snapshot.json")

    assert read_snapshot(path) == snapshot


def test_read_snapshot_reports_bad_json_with_the_filename(tmp_path):
    path = tmp_path / "Account.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(SnapshotError, match="Account.json is not valid JSON"):
        read_snapshot(path)


def test_read_snapshot_rejects_json_that_is_not_an_object(tmp_path):
    """Found by mypy --strict, not by a failing test.

    json.loads returns Any, so a file holding a list satisfied this function's
    dict[str, Any] annotation and failed much later as a TypeError from
    ``committed["sobject"]`` — a confusing error far from the real problem.
    """
    path = tmp_path / "Account.json"
    path.write_text('["not", "a", "snapshot"]', encoding="utf-8")

    with pytest.raises(SnapshotError, match="holds a JSON list, not an object"):
        read_snapshot(path)


def test_read_snapshot_rejects_a_bare_json_scalar(tmp_path):
    path = tmp_path / "Account.json"
    path.write_text('"just a string"', encoding="utf-8")

    with pytest.raises(SnapshotError, match="not an object"):
        read_snapshot(path)


def test_unwrap_describe_rejects_a_non_object_result():
    """`sf sobject list` puts an array under 'result'. Feeding the wrong
    command's output here should say so, not fail later."""
    with pytest.raises(SfCliError, match="not an object"):
        unwrap_describe({"status": 0, "result": ["Account", "Contact"]})


def test_read_snapshot_reports_bad_encoding_with_the_filename(tmp_path):
    path = tmp_path / "Account.json"
    path.write_bytes(b'{"org": "\xff\xfe"}')

    with pytest.raises(SnapshotError, match="Account.json is not valid UTF-8"):
        read_snapshot(path)


# --- decoding the CLI's stdout (SFM-13b) -------------------------------------
#
# The bug these cover: `subprocess.run(text=True)` with no `encoding=` decodes
# with locale.getpreferredencoding(). `sf --json` emits UTF-8 on every
# platform, so on a cp1252 machine every multi-byte character came back
# mojibake'd and went into the snapshot that way.
#
# Nothing else in this file reaches the decode: every other test hands
# `fetch_describe` a CompletedProcess whose stdout is already a `str`, so the
# decoding step has been mocked away. These two put it back.

# U+0421 CYRILLIC CAPITAL LETTER ES -- indistinguishable from Latin "C" on
# screen, which is why the real occurrence went unnoticed. UTF-8 encodes it as
# D0 A1; cp1252 reads those two bytes as "Ð" + "¡".
CYRILLIC_ES = "\u0421"

_NON_ASCII_DESCRIBE = {
    "name": "Account",
    "fields": [
        {
            "name": "Shared_Data__c",
            "label": f"{CYRILLIC_ES}licks",
            "type": "picklist",
            "picklistValues": [{"value": f"{CYRILLIC_ES}TR", "active": True}],
        }
    ],
}

# What the child process actually writes to the pipe: bytes, not str.
_CHILD_STDOUT_BYTES = json.dumps(
    {"status": 0, "result": _NON_ASCII_DESCRIBE}, ensure_ascii=False
).encode("utf-8")


def _fake_run_decoding_like_subprocess(command, encoding=None, **kwargs):
    """Stand in for subprocess.run, including the part that decodes.

    The real `run` decodes the child's bytes with `encoding`, falling back to
    the platform's preferred encoding when the caller names none. The fallback
    here is pinned to cp1252 rather than read from the live locale, so this
    fails on a UTF-8 Linux runner too: the defect is "we did not say which
    encoding", not "this machine happens to be Windows". A locale-dependent
    test would pass in CI and leave the bug in place.
    """
    return subprocess.CompletedProcess(
        args=command,
        returncode=0,
        stdout=_CHILD_STDOUT_BYTES.decode(encoding or "cp1252"),
        stderr="",
    )


def test_stdout_is_decoded_as_utf8_not_the_platform_default():
    """The picklist value must survive as U+0421, not as its cp1252 mojibake.

    Asserted on the built snapshot because that is what gets committed and
    diffed: a corrupted value here makes `check` report CRITICAL "value
    removed" against an org that never changed.
    """
    with (
        patch("soqlmodel.extract.shutil.which", return_value="/usr/bin/sf"),
        patch("soqlmodel.extract.subprocess.run", _fake_run_decoding_like_subprocess),
    ):
        describe = fetch_describe("Account", "alias", "68.0")

    snapshot = build_snapshot(describe, org="alias")
    field = snapshot["fields"][0]

    assert field["picklistValues"] == [f"{CYRILLIC_ES}TR"]
    assert field["label"] == f"{CYRILLIC_ES}licks"
    # Spelled out so a failure names the actual defect rather than showing two
    # strings that look identical in the diff.
    assert "\u00d0" not in field["label"], "UTF-8 bytes were decoded as cp1252"


def test_fetch_describe_names_the_encoding_explicitly():
    """Pins the mechanism, so the guard cannot be lost to a refactor that
    happens to keep the behaviour on a UTF-8 machine."""
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"status": 0, "result": {"name": "Account"}}', stderr=""
    )

    with (
        patch("soqlmodel.extract.shutil.which", return_value="/usr/bin/sf"),
        patch("soqlmodel.extract.subprocess.run", return_value=completed) as run,
    ):
        fetch_describe("Account", "alias", "68.0")

    assert run.call_args.kwargs["encoding"] == "utf-8"


def test_write_and_read_agree_on_non_ascii(tmp_path):
    """The pair, not each half.

    `test_write_snapshot_writes_non_ascii_literally` checks the bytes on disk
    and `test_read_snapshot_round_trips_what_write_snapshot_wrote` checks the
    round trip, but that one uses an ASCII-only snapshot -- so nothing asserted
    that our own reader survives what our own writer emits for a real label.
    A regression to `path.read_text()` (no encoding) in read_snapshot would
    pass every other test in this file on a UTF-8 machine.
    """
    snapshot = {
        "org": "alias",
        "sobject": "Account",
        "fields": [{"name": "Shared_Data__c", "picklistValues": [f"{CYRILLIC_ES}TR", "Größe"]}],
    }

    path = write_snapshot(snapshot, tmp_path / "Account.json")

    assert read_snapshot(path) == snapshot
    # D13 still holds for the same file: no BOM was introduced by writing it.
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")


# --- the credential source (SFM-13c) ------------------------------------------


def _clear_credentials(monkeypatch):
    for var in CREDENTIAL_ENV_VARS.values():
        monkeypatch.delenv(var, raising=False)


def _set_credentials(monkeypatch, **overrides):
    values = {
        "SOQLMODEL_SF_USERNAME": "user@example.com",
        "SOQLMODEL_SF_CONSUMER_KEY": "key",
        "SOQLMODEL_SF_PRIVATEKEY_FILE": "/nowhere/server.key",
        "SOQLMODEL_SF_DOMAIN": "example--sandbox.my",
    }
    values.update(overrides)
    for var, value in values.items():
        monkeypatch.setenv(var, value)


def test_every_missing_credential_variable_is_named_at_once(monkeypatch):
    """All four, not just the first. Reporting one at a time turns setting
    this up into four failed runs."""
    _clear_credentials(monkeypatch)

    with pytest.raises(CredentialError) as exc:
        require_credentials()

    for var in CREDENTIAL_ENV_VARS.values():
        assert var in str(exc.value)


def test_a_single_missing_credential_variable_is_named(monkeypatch):
    _set_credentials(monkeypatch)
    monkeypatch.delenv("SOQLMODEL_SF_CONSUMER_KEY")

    with pytest.raises(CredentialError, match="SOQLMODEL_SF_CONSUMER_KEY"):
        require_credentials()


def test_a_blank_credential_variable_counts_as_missing(monkeypatch):
    """An exported-but-empty variable is the shape a broken CI secret takes."""
    _set_credentials(monkeypatch, SOQLMODEL_SF_DOMAIN="   ")

    with pytest.raises(CredentialError, match="SOQLMODEL_SF_DOMAIN"):
        require_credentials()


def test_the_credential_error_never_echoes_a_value(monkeypatch):
    _set_credentials(monkeypatch, SOQLMODEL_SF_CONSUMER_KEY="")

    with pytest.raises(CredentialError) as exc:
        require_credentials()

    assert "user@example.com" not in str(exc.value)
    assert "/nowhere/server.key" not in str(exc.value)


def test_the_live_gate_variables_are_not_reused(monkeypatch):
    """SOQLMODEL_LIVE_* gates the test suite; these configure an extractor.
    Sharing them would make one variable do two unrelated jobs."""
    _clear_credentials(monkeypatch)
    monkeypatch.setenv("SOQLMODEL_LIVE_USERNAME", "user@example.com")
    monkeypatch.setenv("SOQLMODEL_LIVE_CONSUMER_KEY", "key")
    monkeypatch.setenv("SOQLMODEL_LIVE_PRIVATEKEY_FILE", "/nowhere/server.key")
    monkeypatch.setenv("SOQLMODEL_LIVE_DOMAIN", "example--sandbox.my")

    with pytest.raises(CredentialError, match="SOQLMODEL_SF_USERNAME"):
        require_credentials()


def test_credentials_are_checked_before_the_extra_is_imported(monkeypatch):
    """Ordering, so someone without the extra and without variables is told
    about the variables rather than being sent to install a package first."""
    _clear_credentials(monkeypatch)

    with pytest.raises(CredentialError, match="environment variables"):
        fetch_describe_via_credentials("Account", "68.0")


def test_a_missing_extra_names_the_install_command(monkeypatch):
    """Absence is detected with find_spec, never by attempting an import.

    That is execute.py's mechanism and the reason it holds: a static
    `import simple_salesforce` would make mypy --strict fail wherever the
    extra is not installed, which is exactly how CI runs.
    """
    _set_credentials(monkeypatch)
    monkeypatch.setattr("soqlmodel.extract.find_spec", lambda name: None)

    with pytest.raises(CredentialError, match=r"pip install"):
        fetch_describe_via_credentials("Account", "68.0")


def test_no_module_in_the_package_imports_the_optional_extra_statically():
    """The invariant execute.py declares, asserted rather than trusted.

    A static import is invisible locally (the extra is installed) and fails
    mypy --strict in CI (it is not). Cheaper to assert here than to rediscover
    on a red build.
    """
    import pathlib

    offenders = []
    for module in sorted(pathlib.Path("src/soqlmodel").glob("*.py")):
        for number, line in enumerate(module.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import simple_salesforce", "from simple_salesforce")):
                offenders.append(f"{module.name}:{number}")

    assert offenders == [], f"static import of the optional extra at {offenders}"


# --- the dispatcher -----------------------------------------------------------


def test_the_default_source_is_sf():
    assert SOURCE_SF == "sf"
    assert SOURCES[0] == SOURCE_SF


def test_extract_describe_routes_to_the_sf_path(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "soqlmodel.extract.fetch_describe",
        lambda sobject, org, api_version: (
            seen.update(sobject=sobject, org=org, api_version=api_version) or {"name": sobject}
        ),
    )

    result = extract_describe("Account", org="alias", source=SOURCE_SF, api_version="68.0")

    assert result == {"name": "Account"}
    assert seen == {"sobject": "Account", "org": "alias", "api_version": "68.0"}


def test_extract_describe_routes_to_the_credential_path_and_ignores_org(monkeypatch):
    """org is a label on this source (D21): it is recorded in the snapshot but
    plays no part in reaching the org."""
    seen = {}
    monkeypatch.setattr(
        "soqlmodel.extract.fetch_describe_via_credentials",
        lambda sobject, api_version: (
            seen.update(sobject=sobject, api_version=api_version) or {"name": sobject}
        ),
    )

    result = extract_describe(
        "Account", org="ignored", source=SOURCE_CREDENTIALS, api_version="68.0"
    )

    assert result == {"name": "Account"}
    assert seen == {"sobject": "Account", "api_version": "68.0"}


def test_an_unknown_source_is_a_bug_not_a_user_error():
    """ValueError, not SoqlModelError: the CLI constrains --source with
    `choices`, so an unknown value can only come from a caller with a typo,
    and the CLI must not dress a bug up as exit 2 (D11)."""
    with pytest.raises(ValueError, match="unknown extraction source"):
        extract_describe("Account", org="a", source="nope", api_version="68.0")

    assert not issubclass(ValueError, SoqlModelError)


def test_both_sources_produce_the_same_snapshot_from_the_same_content(monkeypatch):
    """The claim SFM-13a made against a live org, pinned offline.

    The sf side arrives wrapped in the CLI envelope and the credential side
    does not; after each path is done, build_snapshot must see the same thing.
    """
    describe = {
        "name": "Account",
        "fields": [
            {
                "name": "Name",
                "label": "Account Name",
                "type": "string",
                "nillable": False,
                "length": 255,
            }
        ],
    }

    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps({"status": 0, "result": describe}), stderr=""
    )
    with (
        patch("soqlmodel.extract.shutil.which", return_value="/usr/bin/sf"),
        patch("soqlmodel.extract.subprocess.run", return_value=completed),
    ):
        via_sf = extract_describe("Account", org="alias", source=SOURCE_SF, api_version="68.0")

    # The REST payload: the same describe, with no envelope around it.
    monkeypatch.setattr(
        "soqlmodel.extract.fetch_describe_via_credentials",
        lambda sobject, api_version: json.loads(json.dumps(describe)),
    )
    via_credentials = extract_describe(
        "Account", org="alias", source=SOURCE_CREDENTIALS, api_version="68.0"
    )

    assert via_sf == via_credentials
    assert build_snapshot(via_sf, org="alias") == build_snapshot(via_credentials, org="alias")
