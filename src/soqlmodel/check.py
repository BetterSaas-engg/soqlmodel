"""Stage 5: diff a committed snapshot against the org and report drift.

:func:`diff_snapshots` is pure — two snapshot dicts in, a list of changes out,
no network. :func:`check` is the thin caller that re-extracts the live side.
The seam matters: every classification rule below is testable with two dicts.

Severity is the whole product here. CRITICAL means a pipeline is broken or is
about to return wrong numbers; WARNING means something moved that no existing
query depends on. Getting that line wrong in either direction is how a drift
detector becomes noise and then gets muted (D10).
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from soqlmodel.config import Config
from soqlmodel.describe import SNAPSHOT_FORMAT_VERSION, build_snapshot, missing_fields
from soqlmodel.extract import fetch_describe


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"


_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1}


@dataclass(frozen=True)
class Change:
    """One difference between the committed snapshot and the org."""

    severity: Severity
    sobject: str
    field: str
    message: str

    def render(self) -> str:
        where = f"{self.sobject}.{self.field}" if self.field else self.sobject
        return f"{self.severity:<8}  {where}: {self.message}"


def _sort_key(change: Change) -> tuple[int, str, str, str]:
    return (
        _SEVERITY_ORDER[change.severity],
        change.sobject,
        change.field,
        change.message,
    )


def _by_name(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {field["name"]: field for field in snapshot.get("fields") or ()}


def _picklist(field: dict[str, Any]) -> set[str]:
    return set(field.get("picklistValues") or ())


def _compare_field(
    sobject: str, name: str, committed: dict[str, Any], live: dict[str, Any]
) -> list[Change]:
    """Classify every difference between one field's two versions."""
    changes = []

    def critical(message: str) -> None:
        changes.append(Change(Severity.CRITICAL, sobject, name, message))

    def warning(message: str) -> None:
        changes.append(Change(Severity.WARNING, sobject, name, message))

    # --- CRITICAL: breaks loudly, or silently returns the wrong thing -------

    if committed.get("type") != live.get("type"):
        critical(f"type changed from {committed.get('type')!r} to {live.get('type')!r}")

    was, now = _picklist(committed), _picklist(live)
    for value in sorted(was - now):
        # A removed value means a mapping keyed on it is now dead (D3/D4).
        critical(f'value removed "{value}"')

    if committed.get("filterable") is True and live.get("filterable") is False:
        critical("no longer filterable; existing WHERE clauses will fail")

    if committed.get("sortable") is True and live.get("sortable") is False:
        critical("no longer sortable; existing ORDER BY clauses will fail")

    if not committed.get("deprecatedAndHidden") and live.get("deprecatedAndHidden"):
        critical("deprecated and hidden")

    # --- WARNING: worth knowing, breaks nothing that already runs -----------

    for value in sorted(now - was):
        # Silently unmapped rather than loudly broken — the reason D3 stores
        # picklist values at all.
        warning(f'value added "{value}"')

    if committed.get("nillable") != live.get("nillable"):
        warning(
            f"nillable changed from {committed.get('nillable')} to {live.get('nillable')}"
        )

    for prop in ("length", "precision", "scale"):
        if committed.get(prop) != live.get(prop):
            warning(f"{prop} changed from {committed.get(prop)} to {live.get(prop)}")

    return changes


def diff_snapshots(committed: dict[str, Any], live: dict[str, Any]) -> list[Change]:
    """Compare a committed snapshot against a freshly built one.

    Pure: no network, no file access. Returns changes sorted CRITICAL first,
    then by sObject, field and message, so a report is stable across runs.

    A ``format_version`` mismatch short-circuits: our format moved, not the
    org's schema, and diffing across formats would report our own reformatting
    as drift (D6). The result is a single change telling the user to
    regenerate.
    """
    sobject = committed.get("sobject") or live.get("sobject") or "?"

    committed_version = committed.get("format_version", SNAPSHOT_FORMAT_VERSION)
    live_version = live.get("format_version", SNAPSHOT_FORMAT_VERSION)
    if committed_version != live_version:
        return [
            Change(
                Severity.CRITICAL,
                sobject,
                "",
                f"snapshot format changed from {committed_version} to {live_version}; "
                "regenerate the snapshot — this is our format moving, not the org",
            )
        ]

    was = _by_name(committed)
    now = _by_name(live)

    changes = []

    # A field the project declared that the org no longer has. missing_fields
    # covers the scoped case (the declaration outlived the field); the set
    # difference covers a field that was snapshotted and has since gone (D9).
    gone = set(was) - set(now) | set(missing_fields(live))
    for name in sorted(gone):
        changes.append(
            Change(
                Severity.CRITICAL,
                sobject,
                name,
                "field no longer exists in the org",
            )
        )

    for name in sorted(set(now) - set(was)):
        changes.append(
            Change(Severity.WARNING, sobject, name, "new field appeared")
        )

    for name in sorted(set(was) & set(now)):
        changes.extend(_compare_field(sobject, name, was[name], now[name]))

    changes.sort(key=_sort_key)
    return changes


def format_report(changes: list[Change]) -> str:
    """Render changes as plain text, CRITICAL first."""
    if not changes:
        return "No drift."
    return "\n".join(change.render() for change in changes)


def has_critical(changes: list[Change]) -> bool:
    return any(change.severity is Severity.CRITICAL for change in changes)


def exit_code(changes: list[Change]) -> int:
    """1 when anything CRITICAL is present, else 0.

    WARNINGs alone do not fail the build. A picklist value being added is real
    information, but it is not a reason to block a deploy — and a check that
    blocks on things people cannot act on immediately is a check people mute,
    at which point the CRITICALs stop being seen either (D10).
    """
    return 1 if has_critical(changes) else 0


def check(config: Config, snapshot_path: str | Path) -> list[Change]:
    """Re-extract the live schema and diff it against a committed snapshot.

    The only part of this module that touches the org. The committed snapshot
    names the sObject; the config supplies the org and the declared scope.

    ``strict=False`` on the live build is load-bearing: a declared field that
    has vanished must arrive as a CRITICAL line, not as an exception (D9).
    """
    snapshot_path = Path(snapshot_path)
    committed = json.loads(snapshot_path.read_text(encoding="utf-8"))

    sobject = committed["sobject"]
    org = config.org or committed["org"]
    scope = config.scope_for(sobject)

    describe = fetch_describe(sobject, org)
    live = build_snapshot(describe, org=org, fields=scope, strict=False)

    return diff_snapshots(committed, live)
