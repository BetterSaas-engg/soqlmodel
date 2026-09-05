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
import os
import shutil
import subprocess
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from soqlmodel.errors import (
    AuthenticationError,
    CredentialError,
    SfCliError,
    SnapshotError,
)

SOURCE_SF = "sf"
SOURCE_CREDENTIALS = "credentials"
SOURCES = (SOURCE_SF, SOURCE_CREDENTIALS)

INSTALL_HINT = 'pip install "soqlmodel[salesforce]"'

# Credentials for the REST source, read from the environment and nowhere else.
# Deliberately distinct from the SOQLMODEL_LIVE_* names, which gate the live
# test suite: sharing them would mean opting into tests and configuring a
# production extractor with one variable, and the two decisions are not the
# same decision.
#
# The mapping is name -> environment variable, so an error can name the
# variable the user has to set rather than an internal key.
CREDENTIAL_ENV_VARS = {
    "username": "SOQLMODEL_SF_USERNAME",
    "consumer_key": "SOQLMODEL_SF_CONSUMER_KEY",
    "privatekey_file": "SOQLMODEL_SF_PRIVATEKEY_FILE",
    "domain": "SOQLMODEL_SF_DOMAIN",
}


def require_credentials() -> dict[str, str]:
    """Read the four credential variables, or say which ones are missing.

    Every missing variable is reported at once. Reporting only the first turns
    configuring this into four failed runs, and the failure is not interesting
    enough to deserve four.

    Never logs or echoes a value: the message names variables, not contents.

    Raises:
        CredentialError: naming each unset or empty variable.
    """
    found = {key: os.environ.get(var, "") for key, var in CREDENTIAL_ENV_VARS.items()}
    missing = sorted(CREDENTIAL_ENV_VARS[key] for key, value in found.items() if not value.strip())

    if missing:
        raise CredentialError(
            f"--source credentials needs these environment variables, which are "
            f"unset or empty: {', '.join(missing)}. Credentials are read from the "
            f"environment only, never from soqlmodel.toml."
        )
    return found


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

    result = payload["result"]
    if not isinstance(result, dict):
        # Surfaced by mypy --strict, which would not accept returning the Any
        # this indexing produces. Validating beats casting: `sf sobject list`
        # returns a JSON array under "result", so feeding the wrong command's
        # output here is a real mistake to catch by name rather than to let
        # through and have fail later as a confusing TypeError.
        raise SfCliError(
            f"sf returned a {type(result).__name__} under 'result', not an "
            "object; this does not look like a describe payload"
        )
    return result


def fetch_describe(sobject: str, org: str, api_version: str) -> dict[str, Any]:
    """Describe one sobject via the ``sf`` CLI.

    Args are passed as a list, never a shell string — org aliases contain
    spaces (``FULL Sandbox``) and would be split by a shell.

    ``api_version`` is passed through as ``--api-version`` and is required, not
    optional. Left to itself the CLI picks a version from the org and its own
    config, which is how a snapshot taken here ended up describing a different
    field list than one taken through the credential path — drift with no
    schema change behind it (D21).

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
        "--api-version",
        api_version,
        "--json",
    ]

    # check=False: a non-zero exit is reported below with sf's own stderr,
    # which is more use than CalledProcessError's repr.
    #
    # encoding="utf-8" is load-bearing, not decoration. `text=True` alone
    # decodes with locale.getpreferredencoding(), which is cp1252 on a default
    # Windows install. `sf --json` emits UTF-8 on every platform, so on such a
    # machine a multi-byte label came back mojibake'd: U+0421 (Cyrillic ES,
    # bytes D0 A1) arrived as "Ð¡". That corrupted the snapshot silently, made
    # the file differ between a Windows dev and a Linux runner for the same
    # unchanged org, and made `check` report a CRITICAL "value removed" for
    # drift that never happened. Name the producer's encoding; never inherit
    # the reader's (D20).
    completed = subprocess.run(
        command, capture_output=True, text=True, check=False, encoding="utf-8"
    )

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


def _salesforce_class() -> Any:
    """``simple_salesforce.Salesforce``, fetched without importing it statically.

    This module never writes ``import simple_salesforce``, and that is the
    whole point. :mod:`soqlmodel.execute` states the invariant: *nothing in
    soqlmodel imports it, at runtime or for typing*, which is what keeps the
    extra optional. An ``import`` statement here would break it in a way that
    only shows up in CI, because mypy resolves imports against the installed
    environment — and CI installs without the extra.

    So presence is detected with ``find_spec``, exactly as
    ``execute._require_client`` does, and the class is reached through
    ``import_module``. Both take the module name as a *string*, so mypy never
    looks for a stub and the result is ``Any`` in both environments. That is
    the property this needs: a ``# type: ignore`` cannot deliver it, because
    whichever error it silences in one environment it is unused in the other,
    and ``--strict`` fails on the unused one.

    Raises:
        CredentialError: if the extra is not installed, naming the pip command.
    """
    if find_spec("simple_salesforce") is None:
        raise CredentialError(
            f"--source credentials needs simple-salesforce, which is not "
            f"installed. Install it with: {INSTALL_HINT}"
        )
    return import_module("simple_salesforce").Salesforce


AUTH_EXCEPTION_NAME = "SalesforceAuthenticationFailed"


def _is_auth_failure(exc: BaseException) -> bool:
    """Is this simple-salesforce's authentication rejection?

    Matched by **class name across the MRO**, which is the one identification
    that needs no reference to the class at all. The alternatives both fail
    something that matters:

    ``except SalesforceAuthenticationFailed`` needs a static import, which
    SFM-13c established breaks the optional-extra invariant and fails
    ``mypy --strict`` wherever the extra is not installed — which is how CI
    installs.

    Fetching the class through ``import_module``, as :func:`_salesforce_class`
    does, would work in production but could not be *tested* where it counts:
    CI's test job runs without the extra, so no offline test could construct
    the real exception to raise. A guard that only runs on a developer machine
    is the kind this project has been bitten by already (D20).

    Walking the MRO rather than checking ``type(exc).__name__`` means a
    subclass of the real exception is still recognised, and it survives
    upstream moving the class to a different module.
    """
    return any(cls.__name__ == AUTH_EXCEPTION_NAME for cls in type(exc).__mro__)


def fetch_describe_via_credentials(sobject: str, api_version: str) -> dict[str, Any]:
    """Describe one sobject over REST, authenticating from the environment.

    The other half of :func:`fetch_describe`. Same return shape — a raw
    describe dict, ready for ``build_snapshot`` — reached without the ``sf``
    CLI, so `snapshot` and `check` can run in a container or a scheduler.

    There is no envelope to strip here. ``unwrap_describe`` exists because the
    CLI wraps its output in ``{status, result}``; REST returns the describe
    itself, so this path deliberately does not call it. SFM-13a verified the
    two payloads are otherwise identical across every key we consume, which is
    why no normalization layer sits between this and ``build_snapshot``.

    Credentials come from the environment and only from the environment. They
    are never read from ``soqlmodel.toml``, which is a committed file.

    Raises:
        CredentialError: if the extra is not installed, or a variable is unset.
            Both are raised before any network call, naming what is missing.
        AuthenticationError: if the org rejects the credentials.
    """
    credentials = require_credentials()
    salesforce = _salesforce_class()

    try:
        client = salesforce(
            username=credentials["username"],
            consumer_key=credentials["consumer_key"],
            privatekey_file=credentials["privatekey_file"],
            domain=credentials["domain"],
            # Pinned, never negotiated. See D21 and Config.require_api_version.
            version=api_version,
        )
        described = getattr(client, sobject).describe()
    except Exception as exc:
        # Both calls are inside the try because which one authenticates is
        # simple-salesforce's business: the login is lazy in some versions and
        # eager in others, and pinning that down here would couple us to an
        # implementation detail we have no reason to know.
        if not _is_auth_failure(exc):
            raise
        # `exc` carries no credential values: simple-salesforce builds it from
        # the token endpoint's own `error` / `error_description` fields, and
        # hardcodes the URL in it as the literal "authentication_endpoint", so
        # not even the domain rides along. Checked against the real class and
        # confirmed against a live rejection before being included here.
        raise AuthenticationError(
            f"the org rejected these credentials: {exc}. Check the values of "
            f"{', '.join(CREDENTIAL_ENV_VARS.values())}, and that the connected "
            f"app trusts this user."
        ) from exc

    if not isinstance(described, dict):
        raise CredentialError(
            f"describe() for {sobject} returned {type(described).__name__}, not a dict"
        )
    # simple-salesforce hands back an OrderedDict of OrderedDicts. Normalising
    # to plain dicts keeps what reaches build_snapshot identical in type as
    # well as content to what the sf path produces -- so a snapshot cannot
    # differ by which source built it.
    result: dict[str, Any] = json.loads(json.dumps(described))
    return result


def extract_describe(sobject: str, *, org: str, source: str, api_version: str) -> dict[str, Any]:
    """Describe one sobject from whichever source was selected.

    The common signature the ticket asks for: `snapshot` and `check` call this
    and never learn which extractor ran.

    ``org`` is a label under D21. The ``sf`` source additionally uses it as the
    ``--target-org`` alias, which is what keeps existing configs working; the
    credential source ignores it entirely and authenticates from the
    environment.

    Deliberately takes ``source`` and ``api_version`` as plain arguments rather
    than a :class:`Config`. This module is stage 1 and sits below config in the
    import graph; keeping it that way means extraction stays callable without
    a config object at all.

    Raises:
        ValueError: if ``source`` is not a known name. A caller passing a
            typo'd literal is a bug, not a user failure -- the CLI constrains
            the flag to `choices`, so an unknown value cannot arrive from a
            command line (D11).
    """
    if source == SOURCE_SF:
        return fetch_describe(sobject, org, api_version)
    if source == SOURCE_CREDENTIALS:
        return fetch_describe_via_credentials(sobject, api_version)
    raise ValueError(f"unknown extraction source {source!r}; expected one of {', '.join(SOURCES)}")


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
        loaded = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"{path} is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        # A hand-edited snapshot. One line naming the file beats a raw
        # JSONDecodeError, which names neither the file nor the fix.
        raise SnapshotError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(loaded, dict):
        # A latent bug that mypy --strict found: json.loads returns Any, so a
        # snapshot file holding a list or a bare string sailed through this
        # function's dict[str, Any] annotation and failed later as a TypeError
        # from `committed["sobject"]` — a confusing error a long way from the
        # actual problem.
        raise SnapshotError(
            f"{path} holds a JSON {type(loaded).__name__}, not an object; it is not a snapshot"
        )

    return loaded
