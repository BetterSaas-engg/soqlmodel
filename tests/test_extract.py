import json
import subprocess
from unittest.mock import patch

import pytest

from soqlmodel.describe import build_snapshot
from soqlmodel.errors import SfCliError, SnapshotError
from soqlmodel.extract import (
    fetch_describe,
    read_snapshot,
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
        describe = fetch_describe("Account", "alias")

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
        fetch_describe("Account", "alias")

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
