import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from soqlmodel.cli import EXIT_DRIFT, EXIT_ERROR, EXIT_OK, build_parser, main
from soqlmodel.errors import SfCliError
from soqlmodel.extract import extract_describe as real_extract_describe

REPO_ROOT = Path(__file__).resolve().parents[1]

CONFIG = """
org = "FULL Sandbox"
api_version = "68.0"

[objects]
Account = ["Name", "AnnualRevenue"]
Opportunity = ["*"]
"""


def raw_field(name, **overrides):
    base = {
        "name": name,
        "label": name,
        "type": "string",
        "nillable": True,
        "filterable": True,
        "sortable": True,
        "custom": False,
        "calculated": False,
        "length": 255,
        "precision": 0,
        "scale": 0,
        "restrictedPicklist": False,
        "deprecatedAndHidden": False,
    }
    base.update(overrides)
    return base


ORG_SCHEMA = {
    "Account": {
        "name": "Account",
        "fields": [raw_field("Name"), raw_field("AnnualRevenue", type="currency")],
    },
    "Opportunity": {
        "name": "Opportunity",
        "fields": [raw_field("Name"), raw_field("Amount", type="currency")],
    },
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project directory with a config, and a fake org behind it."""
    (tmp_path / "soqlmodel.toml").write_text(CONFIG, encoding="utf-8")

    def fake_fetch(sobject, *, org, source, api_version):
        return ORG_SCHEMA[sobject]

    monkeypatch.setattr("soqlmodel.project.extract_describe", fake_fetch)
    monkeypatch.setattr("soqlmodel.check.extract_describe", fake_fetch)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- happy paths ------------------------------------------------------------


def test_snapshot_writes_a_file_per_object(project, capsys):
    assert main(["snapshot"]) == EXIT_OK

    assert (project / "schema" / "Account.json").is_file()
    assert (project / "schema" / "Opportunity.json").is_file()
    assert "2 snapshot(s) written." in capsys.readouterr().out


def test_generate_writes_the_default_module(project, capsys):
    main(["snapshot"])
    assert main(["generate"]) == EXIT_OK

    source = (project / "models.py").read_text(encoding="utf-8")
    assert "class Account:" in source
    assert "class Opportunity:" in source
    assert "wrote models.py" in capsys.readouterr().out


def test_generate_honours_output(project):
    main(["snapshot"])
    assert main(["generate", "--output", "custom/models.py"]) == EXIT_OK

    assert (project / "custom" / "models.py").is_file()


def test_check_is_clean_against_an_unchanged_org(project, capsys):
    main(["snapshot"])

    assert main(["check"]) == EXIT_OK
    assert "No drift." in capsys.readouterr().out


def test_schema_dir_is_honoured(project):
    assert main(["snapshot", "--schema-dir", "custom_schema"]) == EXIT_OK

    assert (project / "custom_schema" / "Account.json").is_file()


def test_org_override_reaches_the_snapshot(project):
    main(["snapshot", "--org", "qa-sandbox"])

    snapshot = json.loads((project / "schema" / "Account.json").read_text(encoding="utf-8"))
    assert snapshot["org"] == "qa-sandbox"


def test_global_options_work_before_the_subcommand(project):
    assert main(["--schema-dir", "early", "snapshot"]) == EXIT_OK

    assert (project / "early" / "Account.json").is_file()


def test_global_options_work_after_the_subcommand(project):
    assert main(["snapshot", "--schema-dir", "late"]) == EXIT_OK

    assert (project / "late" / "Account.json").is_file()


def test_the_option_after_the_subcommand_wins(project):
    """Documented precedence, pinned. Not left to argparse to decide for us."""
    assert main(["--schema-dir", "early", "snapshot", "--schema-dir", "late"]) == EXIT_OK

    assert (project / "late" / "Account.json").is_file()
    assert not (project / "early").exists()


@pytest.mark.parametrize(
    ("option", "attribute"),
    [("--schema-dir", "schema_dir"), ("--org", "org"), ("--config", "config")],
)
def test_every_global_option_resolves_to_the_later_occurrence(option, attribute):
    """One rule for all three, so precedence never depends on which you used."""
    args = build_parser().parse_args([option, "before", "snapshot", option, "after"])

    assert getattr(args, attribute) == "after"


@pytest.mark.parametrize(
    ("option", "attribute"),
    [("--schema-dir", "schema_dir"), ("--org", "org"), ("--config", "config")],
)
def test_a_global_option_given_only_before_survives_the_subparser(option, attribute):
    """The other half: the subparser copy must not overwrite it with a default."""
    args = build_parser().parse_args([option, "before", "snapshot"])

    assert getattr(args, attribute) == "before"


def test_the_help_documents_the_precedence(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])

    assert "the one after COMMAND wins" in capsys.readouterr().out


# --- exit codes -------------------------------------------------------------


def test_check_exits_one_on_critical_drift(project, capsys):
    main(["snapshot"])

    path = project / "schema" / "Account.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["fields"].append({"name": "Vanished__c", "type": "string"})
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    assert main(["check"]) == EXIT_DRIFT
    assert "CRITICAL" in capsys.readouterr().out


def test_check_exits_zero_on_warnings_alone(project, capsys):
    main(["snapshot"])

    path = project / "schema" / "Account.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    for field in snapshot["fields"]:
        if field["name"] == "Name":
            field["length"] = 1
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    assert main(["check"]) == EXIT_OK
    assert "WARNING" in capsys.readouterr().out


def test_no_command_prints_help_and_exits_two(project, capsys):
    assert main([]) == EXIT_ERROR
    assert "COMMAND" in capsys.readouterr().out


# --- user errors are one line, never a traceback ----------------------------


def test_a_missing_config_is_a_clean_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main(["snapshot"]) == EXIT_ERROR

    captured = capsys.readouterr()
    assert "no config at" in captured.err
    assert "Traceback" not in captured.err
    assert len(captured.err.strip().splitlines()) == 1


def test_a_missing_snapshot_is_a_clean_error(project, capsys):
    # check before snapshot
    assert main(["check"]) == EXIT_ERROR

    captured = capsys.readouterr()
    assert "run snapshot first" in captured.err
    assert "Traceback" not in captured.err
    assert len(captured.err.strip().splitlines()) == 1


def test_generate_without_snapshots_is_a_clean_error(project, capsys):
    assert main(["generate"]) == EXIT_ERROR

    captured = capsys.readouterr()
    assert "run snapshot first" in captured.err
    assert "Traceback" not in captured.err


def test_an_unreachable_org_is_a_clean_error(project, monkeypatch, capsys):
    def unreachable(sobject, *, org, source, api_version):
        raise SfCliError("sf sobject describe exited 1: No org found for alias 'nope'")

    monkeypatch.setattr("soqlmodel.project.extract_describe", unreachable)

    assert main(["snapshot"]) == EXIT_ERROR

    captured = capsys.readouterr()
    assert "No org found" in captured.err
    assert "Traceback" not in captured.err
    assert len(captured.err.strip().splitlines()) == 1


def test_a_declared_field_the_org_lacks_is_a_clean_error(project, capsys):
    (project / "soqlmodel.toml").write_text(
        'org = "FULL Sandbox"\napi_version = "68.0"\n\n[objects]\nAccount = ["Name", "Nope__c"]\n',
        encoding="utf-8",
    )

    assert main(["snapshot"]) == EXIT_ERROR

    captured = capsys.readouterr()
    assert "Nope__c" in captured.err
    assert "Traceback" not in captured.err


def test_a_malformed_config_is_a_clean_error(project, capsys):
    (project / "soqlmodel.toml").write_text("org = [unclosed", encoding="utf-8")

    assert main(["snapshot"]) == EXIT_ERROR

    captured = capsys.readouterr()
    assert "not valid TOML" in captured.err
    assert "Traceback" not in captured.err


# --- a bug is not a user error ----------------------------------------------
#
# The other half of the contract above. Every test so far proves a user error
# reads as one line; these prove a *defect* does not get the same treatment.
# The catch in main() is narrow so that a stray ValueError from the query
# builder or the generator reaches the user as a traceback, not as a tidy
# "soqlmodel: ..." and exit 2 (D11).


def test_an_unexpected_valueerror_is_not_converted(project, monkeypatch, capsys):
    def buggy(*args, **kwargs):
        raise ValueError("bug in the snapshot builder")

    monkeypatch.setattr("soqlmodel.project.build_snapshot", buggy)

    with pytest.raises(ValueError, match="bug in the snapshot builder"):
        main(["snapshot"])

    assert "soqlmodel:" not in capsys.readouterr().err


def test_an_unexpected_typeerror_is_not_converted(project, monkeypatch):
    def buggy(*args, **kwargs):
        raise TypeError("unsupported operand")

    monkeypatch.setattr("soqlmodel.project.build_snapshot", buggy)

    with pytest.raises(TypeError, match="unsupported operand"):
        main(["snapshot"])


def test_a_bug_reaches_the_terminal_as_a_traceback(project):
    """End to end, through the real entry point: a defect prints a traceback.

    In-process the exception merely escapes `main`; this is the proof that
    what a user actually sees is a stack trace and not exit 2.
    """
    injected = (
        "import sys\n"
        "import soqlmodel.project as project\n"
        "project.extract_describe = lambda sobject, **kw: {'name': sobject, 'fields': []}\n"
        "def buggy(*args, **kwargs):\n"
        "    raise ValueError('bug in the snapshot builder')\n"
        "project.build_snapshot = buggy\n"
        "from soqlmodel.cli import run\n"
        "run()\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", injected, "snapshot"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )

    assert "Traceback (most recent call last)" in result.stderr
    assert "ValueError: bug in the snapshot builder" in result.stderr
    assert "soqlmodel: bug" not in result.stderr
    assert result.returncode != EXIT_ERROR


def test_the_catch_stays_narrow():
    """Pins the tuple itself, so widening it is a deliberate edit with a diff."""
    from soqlmodel.cli import _EXPECTED_ERRORS
    from soqlmodel.errors import SoqlModelError

    assert _EXPECTED_ERRORS == (SoqlModelError, OSError)
    assert not issubclass(SoqlModelError, ValueError)


def test_a_corrupt_snapshot_is_still_a_clean_error(project, capsys):
    """The flip side: JSONDecodeError is a ValueError, and used to be caught
    by the wide tuple. It has to stay a one-liner on its own merits."""
    main(["snapshot"])
    (project / "schema" / "Account.json").write_text("{not json", encoding="utf-8")

    assert main(["check"]) == EXIT_ERROR

    captured = capsys.readouterr()
    assert "is not valid JSON" in captured.err
    assert "Traceback" not in captured.err


def test_a_bom_in_a_snapshot_is_a_clean_error(project, capsys):
    main(["snapshot"])
    path = project / "schema" / "Account.json"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    assert main(["check"]) == EXIT_ERROR

    captured = capsys.readouterr()
    assert "re-run snapshot" in captured.err
    assert "Traceback" not in captured.err


def test_an_unknown_command_exits_two(project):
    # argparse's own usage error path, which also exits 2.
    with pytest.raises(SystemExit) as exc:
        main(["nonsense"])

    assert exc.value.code == EXIT_ERROR


# --- help and version -------------------------------------------------------


def test_help_documents_the_exit_codes(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])

    out = capsys.readouterr().out
    assert "exit codes:" in out
    assert "1  check found CRITICAL drift" in out


def test_version_prints_the_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == EXIT_OK
    assert "soqlmodel" in capsys.readouterr().out


# --- packaging --------------------------------------------------------------


@pytest.mark.slow
def test_the_wheel_declares_the_console_script(tmp_path):
    """The entry point must survive packaging, like py.typed before it."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("uv is needed to build the wheel; this check must not be skipped")

    result = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheel = next(iter(tmp_path.glob("*.whl")))
    with zipfile.ZipFile(wheel) as archive:
        # Located by suffix rather than by name: hardcoding the version makes
        # this test fail on the next bump for a reason unrelated to what it checks.
        name = next(n for n in archive.namelist() if n.endswith(".dist-info/entry_points.txt"))
        entry_points = archive.read(name).decode()

    assert "[console_scripts]" in entry_points
    assert "soqlmodel = soqlmodel.cli:main" in entry_points


@pytest.mark.slow
def test_the_installed_entry_point_runs(project):
    """Invoke the real console script, not python -m."""
    executable = shutil.which("soqlmodel")
    if executable is None:
        pytest.fail("the 'soqlmodel' console script is not on PATH")

    result = subprocess.run([executable, "--version"], capture_output=True, text=True, check=False)

    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "soqlmodel" in result.stdout


def test_python_m_works_too():
    result = subprocess.run(
        [sys.executable, "-m", "soqlmodel.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == EXIT_OK
    assert "soqlmodel" in result.stdout


# --- --source (SFM-13c) -------------------------------------------------------


def test_the_default_source_is_sf():
    """Existing invocations keep the behaviour they had before the flag."""
    assert build_parser().parse_args(["snapshot"]).source == "sf"
    assert build_parser().parse_args(["check"]).source == "sf"


def test_source_is_accepted_on_snapshot_and_check():
    assert (
        build_parser().parse_args(["snapshot", "--source", "credentials"]).source == "credentials"
    )
    assert build_parser().parse_args(["check", "--source", "credentials"]).source == "credentials"


def test_generate_has_no_source_flag():
    """It reads committed files and never reaches an org."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["generate", "--source", "credentials"])


def test_an_unknown_source_is_a_usage_error_not_a_traceback():
    """argparse `choices` catches it, so the dispatcher's ValueError is
    unreachable from a command line."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["snapshot", "--source", "nope"])


def test_the_selected_source_reaches_the_extractor(project, monkeypatch):
    seen = {}

    def fake_fetch(sobject, *, org, source, api_version):
        seen["source"] = source
        return ORG_SCHEMA[sobject]

    monkeypatch.setattr("soqlmodel.project.extract_describe", fake_fetch)

    main(["snapshot", "--source", "credentials"])

    assert seen["source"] == "credentials"


def test_the_default_source_reaches_the_extractor_as_sf(project, monkeypatch):
    seen = {}

    def fake_fetch(sobject, *, org, source, api_version):
        seen["source"] = source
        return ORG_SCHEMA[sobject]

    monkeypatch.setattr("soqlmodel.project.extract_describe", fake_fetch)

    main(["snapshot"])

    assert seen["source"] == "sf"


def test_missing_credentials_are_a_clean_error_naming_the_variables(project, monkeypatch, capsys):
    """Exit 2 and one line, at the origin, before any network call."""
    # The project fixture stubs the extractor; put the real one back, or this
    # would assert against a fake and prove nothing.
    monkeypatch.setattr("soqlmodel.project.extract_describe", real_extract_describe)

    for var in (
        "SOQLMODEL_SF_USERNAME",
        "SOQLMODEL_SF_CONSUMER_KEY",
        "SOQLMODEL_SF_PRIVATEKEY_FILE",
        "SOQLMODEL_SF_DOMAIN",
    ):
        monkeypatch.delenv(var, raising=False)

    assert main(["snapshot", "--source", "credentials"]) == EXIT_ERROR

    captured = capsys.readouterr()
    assert "SOQLMODEL_SF_USERNAME" in captured.err
    assert "Traceback" not in captured.err
    assert len(captured.err.strip().splitlines()) == 1


def test_a_missing_api_version_is_a_clean_error(project, capsys):
    (project / "soqlmodel.toml").write_text(
        'org = "FULL Sandbox"\n\n[objects]\nAccount = ["Name"]\n', encoding="utf-8"
    )

    assert main(["snapshot"]) == EXIT_ERROR

    captured = capsys.readouterr()
    assert "no api_version configured" in captured.err
    assert "Traceback" not in captured.err


def test_the_reported_version_matches_pyproject():
    """pyproject is the single declaration; nothing may drift from it.

    There is deliberately no `__version__` constant to keep in step -- the CLI
    reads the installed distribution metadata, so there is one number, not two.
    What this catches is the installed metadata going stale against the repo:
    a developer bumps pyproject, the venv still carries the old build, and
    `--version` reports a number the project no longer declares. On a release
    that number is unfixable, because a version on PyPI can never be reused.
    """
    import tomllib
    from importlib.metadata import version

    declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert version("soqlmodel") == declared["project"]["version"]


def test_the_release_workflow_tag_check_would_accept_this_version():
    """The guard release.yml runs on a tag push, exercised here.

    It compares GITHUB_REF_NAME with the leading "v" stripped against the
    pyproject version. Getting that pairing wrong is caught only at publish
    time otherwise, which is the worst place to find it.
    """
    import tomllib

    declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = declared["project"]["version"]

    assert f"v{version}".lstrip("v") == version
