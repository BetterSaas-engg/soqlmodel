"""Whole-project operations: one snapshot file per sObject, one model module.

Layout::

    schema/Account.json
    schema/Opportunity.json

One file per sObject, named for it, so a diff in review reads as "the Account
schema changed" rather than as a hunk in the middle of a combined blob. The
directory is configurable; the default is ``schema/`` at the project root.

This module is the orchestration layer over the four stages. It owns no
classification or rendering logic of its own — `describe`, `generate` and
`check` do that, and stay independently testable.
"""

from pathlib import Path
from typing import Any

from soqlmodel.check import Change, Severity, check, sort_changes
from soqlmodel.config import Config
from soqlmodel.describe import build_snapshot
from soqlmodel.errors import ConfigError, SoqlModelError
from soqlmodel.extract import fetch_describe, read_snapshot, write_snapshot
from soqlmodel.generate import write_combined_module

DEFAULT_SCHEMA_DIR = "schema"


class MissingSnapshotError(SoqlModelError, FileNotFoundError):
    """A configured sObject has no committed snapshot yet.

    Both bases are deliberate. It is a ``SoqlModelError`` because it is a user
    failure the CLI reports as one line, and it stays a ``FileNotFoundError``
    because that is what it literally is — code that already catches the
    latter keeps working.
    """


def snapshot_path(sobject: str, schema_dir: str | Path = DEFAULT_SCHEMA_DIR) -> Path:
    """Where the snapshot for ``sobject`` lives."""
    return Path(schema_dir) / f"{sobject}.json"


def committed_sobjects(schema_dir: str | Path = DEFAULT_SCHEMA_DIR) -> list[str]:
    """sObject names that have a snapshot file on disk, sorted."""
    directory = Path(schema_dir)
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.json"))


def orphaned_snapshots(config: Config, schema_dir: str | Path = DEFAULT_SCHEMA_DIR) -> list[str]:
    """Committed snapshots for sObjects the config no longer declares.

    Reported, never deleted: a user's committed file is not ours to remove,
    and a stale snapshot is information — someone dropped a dependency, and
    that is worth a line in the report.
    """
    declared = set(config.objects)
    return [name for name in committed_sobjects(schema_dir) if name not in declared]


def _ensure_dir(directory: Path) -> None:
    """Create a directory something is about to be written into.

    The one copy of this, shared by both writers.

    No guard against the bare-filename case is needed, which is worth writing
    down because it does not look that way: ``Path("models.py").parent`` is
    ``Path(".")``, and ``exist_ok=True`` already makes mkdir on an existing
    directory a no-op. An earlier version tested for it explicitly. That
    branch could never be observed failing, which is the tell for a guard that
    is not guarding anything.
    """
    directory.mkdir(parents=True, exist_ok=True)


def _require_org(config: Config) -> str:
    if not config.org:
        raise ConfigError('no org configured; set org = "<alias>" in soqlmodel.toml')
    return config.org


def _require_objects(config: Config) -> list[str]:
    if not config.is_scoped:
        raise ConfigError(
            'no sObjects configured; add an [objects] table to soqlmodel.toml, e.g. Account = ["*"]'
        )
    return config.sobjects()


def load_snapshot(sobject: str, schema_dir: str | Path = DEFAULT_SCHEMA_DIR) -> dict[str, Any]:
    """Read one committed snapshot.

    Raises:
        MissingSnapshotError: if the file is not there, naming the command that
            would create it.
        SnapshotError: if the file is there but is not what `snapshot` wrote.
    """
    path = snapshot_path(sobject, schema_dir)
    if not path.is_file():
        raise MissingSnapshotError(
            f"no snapshot for {sobject} at {path}; run snapshot first to create it"
        )
    return read_snapshot(path)


def snapshot_all(config: Config, schema_dir: str | Path = DEFAULT_SCHEMA_DIR) -> list[Path]:
    """Extract every configured sObject and write its scoped snapshot.

    Strict (D9): a declared field the org does not have raises, naming the
    sObject and the field. Declaring a dependency on a field that is not there
    is a mistake to fix now, not a smaller model to discover later.

    Returns the paths written, in sObject order.
    """
    org = _require_org(config)
    sobjects = _require_objects(config)

    directory = Path(schema_dir)
    _ensure_dir(directory)

    written = []
    for sobject in sobjects:
        describe = fetch_describe(sobject, org)
        snapshot = build_snapshot(describe, org=org, fields=config.scope_for(sobject), strict=True)
        written.append(write_snapshot(snapshot, snapshot_path(sobject, directory)))
    return written


def generate_all(
    config: Config,
    schema_dir: str | Path = DEFAULT_SCHEMA_DIR,
    out_path: str | Path = "models.py",
) -> Path:
    """Generate one module holding a class per committed snapshot.

    Every configured sObject must have a snapshot; a missing one raises rather
    than quietly emitting a module without that class. Snapshots on disk for
    sObjects no longer in the config are still included — the file is there and
    someone's code may still import the class. `check` reports them as orphans;
    removing them is the user's call.

    Classes are ordered by sObject name, so the output is byte-identical across
    runs.
    """
    required = config.sobjects()
    for sobject in required:
        # Raises with the "run snapshot first" message.
        load_snapshot(sobject, schema_dir)

    names = sorted(set(required) | set(committed_sobjects(schema_dir)))
    snapshots = [load_snapshot(name, schema_dir) for name in names]

    destination = Path(out_path)
    _ensure_dir(destination.parent)

    return write_combined_module(snapshots, destination, config.line_length)


def check_all(config: Config, schema_dir: str | Path = DEFAULT_SCHEMA_DIR) -> list[Change]:
    """Diff every committed snapshot against a fresh extract.

    Non-strict (D9): a declared field that has been deleted arrives as a
    CRITICAL line rather than an exception. Changes from every sObject are
    aggregated and sorted together, so one CRITICAL anywhere means
    ``exit_code`` returns 1.

    Raises:
        MissingSnapshotError: if a configured sObject has no snapshot yet.
    """
    changes: list[Change] = []

    for sobject in config.sobjects():
        path = snapshot_path(sobject, schema_dir)
        if not path.is_file():
            raise MissingSnapshotError(
                f"no snapshot for {sobject} at {path}; run snapshot first to create it"
            )
        changes.extend(check(config, path))

    for sobject in orphaned_snapshots(config, schema_dir):
        changes.append(
            Change(
                Severity.WARNING,
                sobject,
                "",
                f"{snapshot_path(sobject, schema_dir)} is committed but "
                "no longer declared in soqlmodel.toml; delete it if the "
                "dependency is gone",
            )
        )

    return sort_changes(changes)
