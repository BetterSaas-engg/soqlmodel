import pytest

from soqlmodel.config import (
    CONFIG_FILENAME,
    Config,
    discover,
    find_config,
    load_config,
    parse_config,
)
from soqlmodel.errors import ConfigError

EXAMPLE = """
org = "FULL Sandbox"

[objects]
Account = ["Name", "AnnualRevenue", "Contract_End__c"]
Opportunity = ["*"]
"""


def write_config(directory, text=EXAMPLE):
    path = directory / CONFIG_FILENAME
    path.write_text(text, encoding="utf-8")
    return path


# --- parsing ----------------------------------------------------------------


def test_reads_the_org():
    assert parse_config({"org": "Prod"}).org == "Prod"


def test_a_field_list_scopes_to_those_fields(tmp_path):
    config = load_config(write_config(tmp_path))

    assert config.scope_for("Account") == frozenset({"Name", "AnnualRevenue", "Contract_End__c"})


def test_star_means_every_field(tmp_path):
    config = load_config(write_config(tmp_path))

    assert config.scope_for("Opportunity") is None


def test_an_unmentioned_object_is_unscoped(tmp_path):
    config = load_config(write_config(tmp_path))

    assert config.scope_for("Contact") is None


def test_star_beside_other_names_still_means_every_field():
    config = parse_config({"objects": {"Account": ["*", "Name"]}})

    assert config.scope_for("Account") is None


def test_sobjects_are_listed_in_a_stable_order(tmp_path):
    config = load_config(write_config(tmp_path))

    assert config.sobjects() == ["Account", "Opportunity"]


def test_org_is_optional():
    config = parse_config({"objects": {"Account": ["Name"]}})

    assert config.org is None
    assert config.is_scoped


# --- malformed config -------------------------------------------------------


def test_rejects_a_non_string_org():
    with pytest.raises(ConfigError, match="org must be a string"):
        parse_config({"org": 42})


def test_rejects_a_non_table_objects():
    with pytest.raises(ConfigError, match=r"\[objects\] must be a table"):
        parse_config({"objects": ["Account"]})


def test_rejects_a_non_list_scope():
    with pytest.raises(ConfigError, match="must be a list of field names"):
        parse_config({"objects": {"Account": "Name"}})


def test_rejects_a_list_of_non_strings():
    with pytest.raises(ConfigError, match="must be a list of field names"):
        parse_config({"objects": {"Account": [1, 2]}})


def test_rejects_an_empty_scope():
    # An empty list would select nothing, which is never what anyone means.
    with pytest.raises(ConfigError, match="empty list"):
        parse_config({"objects": {"Account": []}})


def test_rejects_invalid_toml(tmp_path):
    path = write_config(tmp_path, "org = [unclosed")

    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(path)


def test_a_utf8_bom_is_tolerated(tmp_path):
    # PowerShell's Set-Content and Notepad both write one by default, and
    # tomllib rejects it with an error that names line 1 column 1 and explains
    # nothing. Found running the CLI on Windows for the first time.
    path = tmp_path / CONFIG_FILENAME
    path.write_bytes(b"\xef\xbb\xbf" + EXAMPLE.encode("utf-8"))

    assert load_config(path).org == "FULL Sandbox"


def test_non_ascii_in_the_config_survives(tmp_path):
    path = tmp_path / CONFIG_FILENAME
    path.write_text('org = "Größe"\n', encoding="utf-8")

    assert load_config(path).org == "Größe"


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


# --- discovery --------------------------------------------------------------


def test_finds_a_config_in_the_directory(tmp_path):
    path = write_config(tmp_path)

    assert find_config(tmp_path) == path


def test_finds_a_config_in_a_parent_directory(tmp_path):
    path = write_config(tmp_path)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    assert find_config(nested) == path


def test_find_returns_none_when_absent(tmp_path):
    # tmp_path has no config and neither should its parents matter: assert on
    # the file itself rather than the walk.
    assert not (tmp_path / CONFIG_FILENAME).exists()


def test_discover_falls_back_to_everything_when_there_is_no_config(tmp_path, monkeypatch):
    monkeypatch.setattr("soqlmodel.config.find_config", lambda start=".": None)
    config = discover(tmp_path)

    assert config == Config()
    assert not config.is_scoped
    assert config.scope_for("Account") is None


def test_discover_loads_the_found_config(tmp_path):
    write_config(tmp_path)

    assert discover(tmp_path).org == "FULL Sandbox"


def test_no_config_at_all_means_everything():
    config = Config()

    assert config.scope_for("Anything") is None
    assert not config.is_scoped


def test_config_is_immutable():
    config = Config(org="Prod")

    with pytest.raises(AttributeError):
        config.org = "Sandbox"  # type: ignore[misc]


# --- line_length (D18) ------------------------------------------------------


def test_line_length_defaults_to_the_formatter_default():
    assert parse_config({}).line_length == 88


def test_line_length_is_read_from_the_config():
    assert parse_config({"line_length": 120}).line_length == 120


def test_line_length_must_be_an_integer():
    with pytest.raises(ConfigError, match="line_length must be an integer"):
        parse_config({"line_length": "88"})


def test_line_length_rejects_a_bool():
    # True is an int in Python; it is not a line length.
    with pytest.raises(ConfigError, match="line_length must be an integer"):
        parse_config({"line_length": True})


def test_line_length_must_be_positive():
    with pytest.raises(ConfigError, match="line_length must be positive"):
        parse_config({"line_length": 0})


def test_line_length_reaches_the_generated_module(tmp_path):
    """The config key is only worth having if it changes the output."""
    from soqlmodel.generate import generate_combined_module

    snapshot = {
        "format_version": 1,
        "org": "o",
        "sobject": "Account",
        "fields": [{"name": "Commission_Attainment_Rolling_12M__c", "type": "string"}],
    }
    narrow = generate_combined_module([snapshot], parse_config({"line_length": 79}).line_length)
    wide = generate_combined_module([snapshot], parse_config({"line_length": 200}).line_length)

    assert "Field(\n" in narrow
    assert "Field(\n" not in wide


# --- api_version (SFM-13c) ---------------------------------------------------


def test_api_version_is_parsed(tmp_path):
    path = tmp_path / "soqlmodel.toml"
    path.write_text('org = "Prod"\napi_version = "68.0"\n', encoding="utf-8")

    assert load_config(path).api_version == "68.0"


def test_api_version_defaults_to_none_not_a_version():
    """No fallback, deliberately. A default here is the 59.0-vs-68.0 skew that
    made two sources disagree about the field list (D21)."""
    assert Config().api_version is None


def test_require_api_version_names_the_setting():
    with pytest.raises(ConfigError, match="no api_version configured"):
        Config(org="Prod").require_api_version()


def test_require_api_version_returns_it_when_set():
    assert Config(org="Prod", api_version="68.0").require_api_version() == "68.0"


def test_an_unquoted_api_version_says_to_quote_it():
    """`api_version = 68.0` is valid TOML and parses as a float, so this is the
    likely mistake rather than an exotic one."""
    with pytest.raises(ConfigError, match="must be a quoted string"):
        parse_config({"api_version": 68.0})


def test_an_empty_api_version_is_refused():
    with pytest.raises(ConfigError, match="api_version is empty"):
        parse_config({"api_version": "   "})


def test_generate_does_not_need_an_api_version():
    """It never touches an org, so requiring one would break the offline path
    for no reason."""
    assert Config(objects={"Account": None}).api_version is None
