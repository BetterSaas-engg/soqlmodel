"""Project configuration: which sObjects and fields the pipeline depends on.

``soqlmodel.toml`` at the project root declares the dependency::

    org = "FULL Sandbox"

    [objects]
    Account = ["Name", "AnnualRevenue", "Contract_End__c"]
    Opportunity = ["*"]

A list of field names scopes that object to those fields; ``"*"`` means every
field. No config file at all means everything, so the tool works unconfigured.

Scoping is what makes a snapshot a *declaration of dependency* rather than a
mirror of the org (D9). It is also the contract between whoever administers
the org and whoever depends on it.
"""

import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from soqlmodel.errors import ConfigError

CONFIG_FILENAME = "soqlmodel.toml"

# A scope of None means "every field on this object" — the unscoped default.
Scope = frozenset[str] | None

ALL_FIELDS = "*"


@dataclass(frozen=True)
class Config:
    """A parsed ``soqlmodel.toml``. Immutable."""

    org: str | None = None
    objects: dict[str, Scope] = dataclasses.field(default_factory=dict)
    path: Path | None = None

    @property
    def is_scoped(self) -> bool:
        """True when the config names any objects at all."""
        return bool(self.objects)

    def scope_for(self, sobject: str) -> Scope:
        """Fields requested for ``sobject``; None means all of them.

        An object absent from the config is unscoped: with no config, or a
        config that does not mention this object, everything is in scope.
        """
        return self.objects.get(sobject)

    def sobjects(self) -> list[str]:
        """Configured sObject names, in a stable order."""
        return sorted(self.objects)


def _parse_scope(sobject: str, raw: Any) -> Scope:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError(
            f'[objects] {sobject} must be a list of field names, or ["*"]; got {raw!r}'
        )
    if not raw:
        raise ConfigError(
            f"[objects] {sobject} is an empty list, which would select no fields. "
            f'Use ["*"] for every field, or remove the entry.'
        )
    if ALL_FIELDS in raw:
        # "*" is a superset of anything listed beside it.
        return None
    return frozenset(raw)


def parse_config(data: dict[str, Any], path: Path | None = None) -> Config:
    """Build a :class:`Config` from already-parsed TOML."""
    org = data.get("org")
    if org is not None and not isinstance(org, str):
        raise ConfigError(f"org must be a string, got {org!r}")

    raw_objects = data.get("objects", {})
    if not isinstance(raw_objects, dict):
        # ConfigError, not TypeError: the user handed us a malformed document,
        # they did not call a Python API with the wrong type. The distinction
        # decides whether this reaches them as one line or as a traceback (D11).
        raise ConfigError(f"[objects] must be a table, got {raw_objects!r}")

    objects = {name: _parse_scope(name, raw) for name, raw in raw_objects.items()}
    return Config(org=org, objects=objects, path=path)


def load_config(path: str | Path) -> Config:
    """Load and parse a config file.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        ConfigError: if the file is not valid TOML, or is structurally wrong.
    """
    path = Path(path)
    # utf-8-sig, not utf-8: PowerShell's Set-Content and Windows Notepad both
    # write a UTF-8 BOM by default, and tomllib rejects one with "Invalid
    # statement at line 1, column 1" — an error that tells the user nothing
    # about the actual problem. Tolerate the BOM instead.
    text = path.read_text(encoding="utf-8-sig")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    return parse_config(data, path=path)


def find_config(start: str | Path = ".") -> Path | None:
    """Find ``soqlmodel.toml`` in ``start`` or any parent directory."""
    current = Path(start).resolve()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def discover(start: str | Path = ".") -> Config:
    """Load the nearest config, or an unscoped one if there is no file.

    Absent config means everything is in scope: the tool works with no config
    at all, and only starts narrowing once someone declares a dependency.
    """
    path = find_config(start)
    return load_config(path) if path else Config()
