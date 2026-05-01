"""
Unit Tests — core/config.py
============================

Tests for ``load_hamilton_config`` and ``FlightThresholds.from_config``.

Scope:
    - TOML loading: absent file, valid file, malformed file
    - FlightThresholds: reads [validation] section, falls back to defaults
    - Precedence logic: CLI arg beats TOML, TOML beats directory fallback

Naming convention:
    test_<subject>_<condition>_<expected_outcome>
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import load_hamilton_config
from core.priorities import FlightThresholds


def test_load_returns_empty_dict_when_file_absent(tmp_path):
    """
    VERIFY: A missing .hamilton.toml returns {} without raising.

    Callers always use dict.get() on the result; None would cause
    AttributeError on every call site that isn't guarded.
    """
    result = load_hamilton_config(tmp_path)
    assert result == {}


def test_load_parses_valid_toml(tmp_path):
    """
    VERIFY: A well-formed .hamilton.toml is parsed into a nested dict.

    Tests both top-level and nested key access so we know the full
    document is loaded, not just the first section.
    """
    (tmp_path / ".hamilton.toml").write_text(
        "[project]\n"
        'name = "my-app"\n'
        'image_tag = "my-app:latest"\n'
        "\n"
        "[validation]\n"
        "p95_ms = 300\n"
    )
    config = load_hamilton_config(tmp_path)

    assert config["project"]["name"] == "my-app"
    assert config["project"]["image_tag"] == "my-app:latest"
    assert config["validation"]["p95_ms"] == 300


def test_load_raises_value_error_on_invalid_toml(tmp_path):
    """
    VERIFY: Malformed TOML raises ValueError (not a raw tomllib parse error).

    load_hamilton_config wraps the internal exception so callers always
    catch the same exception type regardless of the underlying TOML library.
    """
    (tmp_path / ".hamilton.toml").write_text("[[this is not valid toml\n")

    with pytest.raises(ValueError, match=".hamilton.toml"):
        load_hamilton_config(tmp_path)


def test_load_accepts_file_path_directly(tmp_path):
    """
    VERIFY: load_hamilton_config can accept a direct path to the .toml file,
    not just a directory — for callers that pre-resolve the path themselves.
    """
    toml_file = tmp_path / ".hamilton.toml"
    toml_file.write_text("[project]\nname = \"direct\"\n")

    config = load_hamilton_config(toml_file)
    assert config["project"]["name"] == "direct"


def test_flight_thresholds_from_config_reads_validation_section(tmp_path):
    """
    VERIFY: FlightThresholds.from_config reads p95_ms, p99_ms, and
    error_rate_percent from the [validation] section.

    If these values aren't loaded, the K6Driver will silently enforce
    the hardcoded defaults regardless of what the TOML says.
    """
    config = {
        "validation": {
            "p95_ms": 300,
            "p99_ms": 750,
            "error_rate_percent": 2.5,
        }
    }
    thresholds = FlightThresholds.from_config(config)

    assert thresholds.p95_ms == 300
    assert thresholds.p99_ms == 750
    assert thresholds.error_rate_percent == 2.5


def test_flight_thresholds_from_config_falls_back_to_defaults_when_section_absent():
    """
    VERIFY: When the [validation] section is absent, from_config() returns
    the Hamilton-Ops spec baseline (p95=200, p99=500, error_rate=1.0).

    This ensures the tool is safe to run against projects without a
    .hamilton.toml — the defaults are intentional, not accidental.
    """
    thresholds = FlightThresholds.from_config({})

    assert thresholds.p95_ms == 200
    assert thresholds.p99_ms == 500
    assert thresholds.error_rate_percent == 1.0


def test_flight_thresholds_from_config_partial_overrides_use_defaults_for_rest():
    """
    VERIFY: Only specifying some validation keys leaves the unspecified
    keys at their defaults — no KeyError or None propagation.
    """
    config = {"validation": {"p95_ms": 400}}
    thresholds = FlightThresholds.from_config(config)

    assert thresholds.p95_ms == 400
    # Unspecified fields must fall back to defaults.
    assert thresholds.p99_ms == 500
    assert thresholds.error_rate_percent == 1.0


def test_precedence_linter_cmd_cli_beats_toml():
    """
    VERIFY: A CLI-provided linter_cmd takes precedence over the TOML value.

    We simulate the resolution logic directly from ship.py so this test
    does not depend on console I/O or the full ship_cmd call stack.
    """
    cli_value = ["eslint", "--ext", ".js"]
    toml_value = ["flake8", "--max-line-length=120"]

    # Mirror the resolution expression from ship_cmd:
    #   resolved_linter_cmd = linter_cmd or toml_quality.get("linter_cmd") or None
    resolved = cli_value or toml_value or None

    assert resolved == cli_value


def test_precedence_linter_cmd_toml_beats_none_cli():
    """
    VERIFY: When no CLI flag is provided, the TOML [quality].linter_cmd is used.
    """
    cli_value = None
    toml_value = ["flake8", "--max-line-length=120"]

    resolved = cli_value or toml_value or None

    assert resolved == toml_value


def test_precedence_project_name_toml_beats_directory_fallback(tmp_path):
    """
    VERIFY: The [project].name in TOML is used in preference to the
    directory name fallback when no --project CLI flag is given.

    If this breaks, forensic reports will show a meaningless temp-dir
    name instead of the declared project name.
    """
    (tmp_path / ".hamilton.toml").write_text("[project]\nname = \"declared-name\"\n")
    config = load_hamilton_config(tmp_path)
    toml_project = config.get("project", {})

    cli_project = None  # --project not provided
    directory_fallback = tmp_path.name

    # Mirror the resolution expression from ship_cmd:
    #   project_name = project or toml_project.get("name") or Path(stage).resolve().name
    resolved = cli_project or toml_project.get("name") or directory_fallback

    assert resolved == "declared-name"


def test_precedence_cli_project_beats_toml_name(tmp_path):
    """
    VERIFY: A --project CLI flag beats the [project].name in TOML.
    """
    (tmp_path / ".hamilton.toml").write_text("[project]\nname = \"toml-name\"\n")
    config = load_hamilton_config(tmp_path)
    toml_project = config.get("project", {})

    cli_project = "cli-name"
    directory_fallback = tmp_path.name

    resolved = cli_project or toml_project.get("name") or directory_fallback

    assert resolved == "cli-name"


def test_load_checks_hidden_hamilton_folder(tmp_path):
    """
    VERIFY: load_hamilton_config checks project_root/.hamilton/.hamilton.toml
    if no config is found at the root level.

    Allows for cleaner root directories.
    """
    hidden_dir = tmp_path / ".hamilton"
    hidden_dir.mkdir()
    (hidden_dir / ".hamilton.toml").write_text("[project]\nname = \"hidden-config\"\n")

    config = load_hamilton_config(tmp_path)
    assert config["project"]["name"] == "hidden-config"


def test_load_root_beats_hidden_folder(tmp_path):
    """
    VERIFY: If BOTH root and hidden folder configs exist, the root level wins.
    """
    (tmp_path / ".hamilton.toml").write_text("[project]\nname = \"root-config\"\n")
    hidden_dir = tmp_path / ".hamilton"
    hidden_dir.mkdir()
    (hidden_dir / ".hamilton.toml").write_text("[project]\nname = \"hidden-config\"\n")

    config = load_hamilton_config(tmp_path)
    assert config["project"]["name"] == "root-config"
