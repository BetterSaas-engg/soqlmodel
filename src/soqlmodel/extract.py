"""Stage 1: get a describe payload out of an org, and write snapshots to disk.

This is the only module that touches the outside world. It shells out to the
already-authenticated ``sf`` CLI (DECISIONS.md, D2) rather than authenticating
itself, and hands back a plain dict — everything downstream reads the snapshot
file, never the org.

The subprocess call and the parsing are separate functions on purpose: the
parsing is where the bugs live, and it is testable without an org.
"""

import codecs
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from soqlmodel.errors import SfCliError, SnapshotError


def unwrap_describe(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip the ``sf --json`` envelope from a describe payload.

    The CLI wraps output as ``{"status": 0, "result": {...}}``. A payload with
    no ``result`` key is assumed to be already unwrapped and is returned as-is,
    so callers can feed this either shape.

    Raises:
        SfCliError: if the CLI reported a non-zero status.
    """
    status = payload.get("status", 0)
    if status != 0:
        message = payload.get("message") or payload.get("name") or "no message"
        raise SfCliError(f"sf reported status {status}: {message}")

    if "result" not in payload:
        return payload

    return payload["result"]


def fetch_describe(sobject: str, org: str) -> dict[str, Any]:
    """Describe one sobject via the ``sf`` CLI.

    Args are passed as a list, never a shell string — org aliases contain
    spaces (``FULL Sandbox``) and would be split by a shell.

    Raises:
        SfCliError: if ``sf`` is not on PATH, exits non-zero, or emits output
            that is not JSON.
    """
    # Resolve through PATH ourselves: on Windows `sf` is a .cmd shim, which
    # bare subprocess argv lookup does not find.
    executable = shutil.which("sf")
    if executable is None:
        raise SfCliError(
            "the 'sf' CLI was not found on PATH; install the Salesforce CLI "
            "(https://developer.salesforce.com/tools/salesforcecli)"
        )

    command = [
        executable,
        "sobject",
        "describe",
        "--sobject",
        sobject,
        "--target-org",
        org,
        "--json",
    ]

    # check=False: a non-zero exit is reported below with sf's own stderr,
    # which is more use than CalledProcessError's repr.
    completed = subprocess.run(command, capture_output=True, text=True, check=False)

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip() or "(no stderr)"
        raise SfCliError(
            f"sf sobject describe --sobject {sobject} --target-org {org} "
            f"exited {completed.returncode}: {stderr}"
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SfCliError(f"sf returned output that is not JSON: {exc}") from exc

    return unwrap_describe(payload)


def write_snapshot(snapshot: dict[str, Any], path: str | Path) -> Path:
    """Write a snapshot as JSON: indented, key-sorted, newline-terminated.

    Writes LF explicitly and never lets the platform translate line endings, so
    the same snapshot produces the same bytes on Windows and on CI.

    Non-ASCII is written literally rather than ``\\uXXXX``-escaped, so labels
    read as themselves in a diff (D5). That requires the explicit utf-8
    encoding below — without it this would die on Windows, whose default
    codepage is not UTF-8.
    """
    path = Path(path)
    text = json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def read_snapshot(path: str | Path) -> dict[str, Any]:
    """Read a snapshot file, refusing bytes :func:`write_snapshot` would not write.

    The pair matters: this is the only reader, so the rules about what a
    snapshot file may contain live next to the rules about what one gets
    written as.

    A leading UTF-8 BOM is rejected rather than tolerated. ``soqlmodel.toml``
    is hand-written and does tolerate one, because a Windows editor put it
    there and the user should not have to care. A snapshot is only ever
    written by us, so a BOM means the bytes on disk are no longer the bytes we
    wrote — and reading through it would let `check` report "No drift" about a
    file that does not match what `snapshot` produces. That is a silent wrong
    answer about the one property snapshots exist to have (D13).

    Raises:
        SnapshotError: if the file leads with a BOM, or is not valid JSON.
    """
    path = Path(path)
    raw = path.read_bytes()

    if raw.startswith(codecs.BOM_UTF8):
        raise SnapshotError(
            f"{path} starts with a UTF-8 BOM, so it is not the file soqlmodel "
            "wrote; re-run snapshot to rewrite it"
        )

    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"{path} is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        # A hand-edited snapshot. One line naming the file beats a raw
        # JSONDecodeError, which names neither the file nor the fix.
        raise SnapshotError(f"{path} is not valid JSON: {exc}") from exc
