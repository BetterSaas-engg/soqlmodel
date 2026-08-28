"""SFM-10c: generated output must survive `ruff format` unchanged.

The requirement is *stability*, not short lines. Short lines are one way to get
there and not a sufficient one — a formatter also **joins** things. A wrapped
call that would fit on one line gets collapsed, so "always wrap" fails just as
badly as "never wrap", in the opposite direction.

Both failure modes are asserted below against a real formatter rather than
reasoned about, because the rule being relied on (the magic trailing comma) is
ruff's behaviour, not ours, and could change.

Why this matters at all: a user commits `models.py`, their formatter rewrites
it, the next `soqlmodel generate` writes it back, and every run produces a diff
with no schema change behind it. That is the phantom diff this whole project
exists to prevent, produced by the project itself.
"""

import shutil
import subprocess

import pytest

from soqlmodel.generate import DEFAULT_LINE_LENGTH, generate_combined_module

# Names chosen to straddle every threshold: trivially short, around 88, around
# 100, and long enough to overflow anything. The middle two are real field
# names from a production org.
FIELD_NAMES = [
    "Id",
    "Name",
    "AnnualRevenue",
    "HiBob_Variable_Pay_Annual__c",
    "HiBob_Variable_Pay_Synced_At__c",
    "Commission_Attainment_Rolling_12M__c",
    "Commission_Attainment_Rolling_12M_Actual_Value__c",
]


def snapshot(*names: str) -> dict:
    return {
        "format_version": 1,
        "org": "FULL Sandbox",
        "sobject": "Account",
        "fields": [{"name": name, "type": "string", "label": name} for name in names],
    }


def ruff() -> str:
    found = shutil.which("ruff")
    if found is None:  # pragma: no cover
        pytest.fail("ruff is required for these tests; it must not be skipped")
    return found


def format_with_ruff(source: str, line_length: int, tmp_path) -> str:
    path = tmp_path / "models.py"
    path.write_text(source, encoding="utf-8", newline="\n")

    result = subprocess.run(
        [ruff(), "format", "--line-length", str(line_length), "-q", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    return path.read_text(encoding="utf-8")


# --- the requirement ---------------------------------------------------------


@pytest.mark.parametrize("line_length", [79, 88, 100, 120])
def test_generated_output_is_unchanged_by_ruff_format(line_length, tmp_path):
    """The whole point. Generate at a line length, format at the same one,
    and nothing moves."""
    source = generate_combined_module([snapshot(*FIELD_NAMES)], line_length)

    assert format_with_ruff(source, line_length, tmp_path) == source


@pytest.mark.parametrize("line_length", [79, 88, 100, 120])
def test_formatting_is_idempotent_from_the_second_run_too(line_length, tmp_path):
    """Guards the subtler case: stable on pass one but not pass two."""
    source = generate_combined_module([snapshot(*FIELD_NAMES)], line_length)
    once = format_with_ruff(source, line_length, tmp_path)
    twice = format_with_ruff(once, line_length, tmp_path)

    assert once == twice == source


def test_the_default_is_stable_under_a_default_ruff(tmp_path):
    """No configuration on either side — the path most users are on."""
    source = generate_combined_module([snapshot(*FIELD_NAMES)])

    assert format_with_ruff(source, DEFAULT_LINE_LENGTH, tmp_path) == source


def test_wrapped_fields_survive_a_wider_formatter(tmp_path):
    """The magic trailing comma's job. A wrapped call must not be collapsed
    by a formatter configured wider than we generated for."""
    source = generate_combined_module([snapshot(*FIELD_NAMES)], 79)

    assert format_with_ruff(source, 200, tmp_path) == source


# --- proving the guard can fail ----------------------------------------------


def test_an_unwrapped_long_line_would_be_reformatted(tmp_path):
    """The bug SFM-10c fixed, reproduced. Without wrapping, ruff rewrites it —
    so the tests above are testing something real."""
    long_name = "Commission_Attainment_Rolling_12M_Actual_Value__c"
    unwrapped = (
        "from soqlmodel.fields import Field\n"
        "\n"
        "\n"
        "class Account:\n"
        f'    {long_name}: Field[str] = Field("{long_name}", "string")\n'
    )

    assert format_with_ruff(unwrapped, 88, tmp_path) != unwrapped


def test_a_wrapped_call_without_the_magic_comma_would_be_collapsed(tmp_path):
    """The other direction, and the reason the trailing comma is not styling.
    Drop it and ruff joins the call back onto one line."""
    no_magic_comma = (
        "from soqlmodel.fields import Field\n"
        "\n"
        "\n"
        "class Account:\n"
        "    Name: Field[str] = Field(\n"
        '        "Name", "string"\n'
        "    )\n"
    )

    assert format_with_ruff(no_magic_comma, 88, tmp_path) != no_magic_comma


# --- the shape of the output -------------------------------------------------


def test_short_fields_stay_on_one_line():
    source = generate_combined_module([snapshot("Name")], DEFAULT_LINE_LENGTH)

    assert '    Name: Field[str] = Field("Name", "string")' in source


def test_long_fields_wrap_with_a_trailing_comma():
    long_name = "Commission_Attainment_Rolling_12M_Actual_Value__c"
    source = generate_combined_module([snapshot(long_name)], DEFAULT_LINE_LENGTH)

    assert f"    {long_name}: Field[str] = Field(\n" in source
    assert f'        "{long_name}",\n' in source
    assert '        "string",\n' in source


def test_no_generated_line_exceeds_the_line_length():
    """Not the requirement, but it should hold anyway — and if it ever does not,
    the stability tests above are the ones that matter."""
    for line_length in (79, 88, 100):
        source = generate_combined_module([snapshot(*FIELD_NAMES)], line_length)
        too_long = [line for line in source.splitlines() if len(line) > line_length]

        assert too_long == [], f"at {line_length}: {too_long}"


def test_output_is_still_byte_identical_across_runs():
    """Wrapping must not have cost determinism (D5)."""
    first = generate_combined_module([snapshot(*FIELD_NAMES)], 88)
    second = generate_combined_module([snapshot(*FIELD_NAMES)], 88)

    assert first == second
