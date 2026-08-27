"""The exception hierarchy is load-bearing, so it gets tested like anything else.

The CLI's `except (SoqlModelError, OSError)` is only as narrow as these
relationships make it. If `SoqlModelError` ever gained `ValueError` as a base,
every test in test_cli.py would still pass while the guard silently stopped
guarding — so the relationships are asserted directly (D11).
"""

import pytest

from soqlmodel.errors import (
    ConfigError,
    GenerateError,
    SfCliError,
    SnapshotError,
    SoqlModelError,
)
from soqlmodel.project import MissingSnapshotError

STAGE_ERRORS = [ConfigError, SfCliError, SnapshotError, GenerateError, MissingSnapshotError]


@pytest.mark.parametrize("error", STAGE_ERRORS)
def test_every_stage_error_is_a_soqlmodel_error(error):
    """One catch clause has to reach all of them."""
    assert issubclass(error, SoqlModelError)


@pytest.mark.parametrize("error", [SoqlModelError, *STAGE_ERRORS])
def test_no_error_is_a_valueerror(error):
    """The whole point of the split. A ValueError base would hand the CLI back
    its ability to swallow a bug in the query builder."""
    assert not issubclass(error, ValueError)
    assert not issubclass(error, TypeError)


def test_missing_snapshot_error_is_still_a_filenotfounderror():
    """Both bases are deliberate; code catching the stdlib one keeps working."""
    assert issubclass(MissingSnapshotError, FileNotFoundError)
    assert issubclass(MissingSnapshotError, OSError)


def test_missing_snapshot_error_carries_its_message():
    """FileNotFoundError has opinions about its args. Confirm str() survives
    the multiple inheritance rather than turning into an errno repr."""
    error = MissingSnapshotError("no snapshot for Account; run snapshot first")

    assert str(error) == "no snapshot for Account; run snapshot first"


def test_the_base_is_catchable_as_one_thing():
    with pytest.raises(SoqlModelError):
        raise ConfigError("no org configured")
