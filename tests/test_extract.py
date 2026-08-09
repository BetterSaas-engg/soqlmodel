import json
import subprocess
from unittest.mock import patch

import pytest

from soqlmodel.extract import fetch_describe, unwrap_describe, write_snapshot


def test_unwraps_the_cli_envelope():
    payload = {"status": 0, "result": {"name": "Account", "fields": []}, "warnings": []}

    assert unwrap_describe(payload) == {"name": "Account", "fields": []}


def test_returns_already_unwrapped_payload_as_is():
    payload = {"name": "Account", "fields": []}

    assert unwrap_describe(payload) is payload


def test_raises_on_non_zero_status():
    payload = {"status": 1, "name": "NoOrgFound", "message": "No org found for alias Prod"}

    with pytest.raises(ValueError, match="No org found for alias Prod"):
        unwrap_describe(payload)


def test_raises_on_non_zero_status_even_when_a_result_is_present():
    payload = {"status": 1, "message": "partial failure", "result": {"name": "Account"}}

    with pytest.raises(ValueError):
        unwrap_describe(payload)


def test_error_message_survives_a_payload_with_no_message():
    with pytest.raises(ValueError, match="status 68"):
        unwrap_describe({"status": 68})


def test_builds_the_command_as_a_list_with_the_org_alias_intact():
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"status": 0, "result": {"name": "Account"}}', stderr=""
    )

    with (
        patch("soqlmodel.extract.shutil.which", return_value="/usr/bin/sf"),
        patch("soqlmodel.extract.subprocess.run", return_value=completed) as run,
    ):
        result = fetch_describe("Account", "FULL Sandbox")

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
