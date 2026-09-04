"""Project configuration: which sObjects and fields the pipeline depends on.

``soqlmodel.toml`` at the project root declares the dependency::

    org = "FULL Sandbox"
    api_version = "68.0"

    [objects]
    Account = ["Name", "AnnualRevenue", "Contract_End__c"]
    Opportunity = ["*"]

A list of field names scopes that object to those fields; ``"*"`` means every
field. No config file at all means everything, so the tool works unconfigured.

``api_version`` pins the Salesforce API version every extraction runs against.
It has no default on purpose: the two extraction sources negotiate different
versions when left to themselves, and a snapshot taken at one version against a
snapshot taken at another differs in its *field list* — which `check` reports as
drift that never happened (D21). Required by `snapshot` and `check`, ignored by
`generate`, which never touches an org.

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
from soqlmodel.extract import SOURCE_SF
from soqlmodel.generate import DEFAULT_LINE_LENGTH

CONFIG_FILENAME = "soqlmodel.toml"

# A scope of None means "every field on this object" — the unscoped default.
Scope = frozenset[str] | None

ALL_FIELDS = "*"


@dataclass(frozen=True)
class Config:
    """A parsed ``soqlmodel.toml``. Immutable."""

    # A label recorded in the snapshot. On the `sf` source it doubles as the
    # --target-org alias; on the credential source it is only a name (D21).
    org: str | None = None
    # The pinned Salesforce API version, e.g. "68.0". None means the config did
    # not set one, which is an error at the point of extraction rather than
    # here — `generate` has no use for it and must keep working without one.
    api_version: str | None = None
    # Which extractor runs. Set by the --source flag, never parsed from the
    # toml and never inferred from whether credentials happen to be present in
    # the environment: choosing to talk to an org over the network is an
    # explicit decision, not something a stray variable makes for you (D19/D21).
    source: str = SOURCE_SF
    objects: dict[str, Scope] = dataclasses.field(default_factory=dict)
    path: Path | None = None
    # Matches the formatter the generated module will be checked by. 88 is
    # ruff and Black's default, so most projects never set it (D18).
    line_length: int = DEFAULT_LINE_LENGTH

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

    def require_api_version(self) -> str:
        """The pinned API version, or a ConfigError naming what is missing.

        A method rather than a helper in `project`, because `check` needs the
        same rule and importing it from there would close an import cycle.
        Callers check this before extracting, so a config missing the setting
        fails without having contacted the org at all.
        """
        if not self.api_version:
            raise ConfigError(
                'no api_version configured; set api_version = "68.0" in soqlmodel.toml. '
                "There is deliberately no default: the sf CLI and the credential "
                "source each pick their own version, and two snapshots taken at "
                "different versions differ in their field list, which check "
                "reports as drift that never happened."
            )
        return self.api_version


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

    api_version = data.get("api_version")
    if api_version is not None and not isinstance(api_version, str):
        # Worth its own message: `api_version = 68.0` is valid TOML and parses
        # as a float, so the likely mistake is forgetting the quotes rather
        # than passing something nonsensical. Say which one it is.
        raise ConfigError(f'api_version must be a quoted string like "68.0", got {api_version!r}')
    if api_version is not None and not api_version.strip():
        raise ConfigError('api_version is empty; set it to a version like "68.0"')

    line_length = data.get("line_length", DEFAULT_LINE_LENGTH)
    if isinstance(line_length, bool) or not isinstance(line_length, int):
        raise ConfigError(f"line_length must be an integer, got {line_length!r}")
    if line_length < 1:
        raise ConfigError(f"line_length must be positive, got {line_length}")

    raw_objects = data.get("objects", {})
    if not isinstance(raw_objects, dict):
        # ConfigError, not TypeError: the user handed us a malformed document,
        # they did not call a Python API with the wrong type. The distinction
        # decides whether this reaches them as one line or as a traceback (D11).
        raise ConfigError(f"[objects] must be a table, got {raw_objects!r}")

    objects = {name: _parse_scope(name, raw) for name, raw in raw_objects.items()}
    return Config(
        org=org,
        api_version=api_version,
        objects=objects,
        path=path,
        line_length=line_length,
    )


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
