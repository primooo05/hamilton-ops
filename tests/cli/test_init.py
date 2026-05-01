import pytest
from pathlib import Path
from cli.discovery import _classify_dir
from cli.init import init_cmd

# Thin compatibility shim — detect_project_type logic now lives in
# _classify_dir (cli.discovery). Wrap it so existing test names stay readable.
def detect_project_type(path):
    unit = _classify_dir(path, depth=0)
    return unit.ecosystem if unit else "generic"

def test_detect_project_type_node(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert detect_project_type(tmp_path) == "node"

def test_detect_project_type_python_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    assert detect_project_type(tmp_path) == "python"

def test_detect_project_type_python_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("")
    assert detect_project_type(tmp_path) == "python"

def test_detect_project_type_rust(tmp_path):
    (tmp_path / "Cargo.toml").write_text("")
    assert detect_project_type(tmp_path) == "rust"

def test_detect_project_type_generic(tmp_path):
    (tmp_path / "main.c").write_text("")
    assert detect_project_type(tmp_path) == "generic"


def test_init_cmd_creates_toml_and_script(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    
    init_cmd(tmp_path)
    
    config_file = tmp_path / ".hamilton.toml"
    script_file = tmp_path / "tests" / "p1_validation.js"
    
    assert config_file.exists()
    assert script_file.exists()
    
    content = config_file.read_text()
    assert 'linter_cmd = ["flake8", "."]' in content
    assert 'k6_script = "tests/p1_validation.js"' in content
    assert tmp_path.name in content

def test_init_cmd_respects_existing_files_without_force(tmp_path):
    config_file = tmp_path / ".hamilton.toml"
    config_file.write_text("existing_config")
    
    script_dir = tmp_path / "tests"
    script_dir.mkdir()
    script_file = script_dir / "p1_validation.js"
    script_file.write_text("existing_script")
    
    init_cmd(tmp_path, force=False)
    
    assert config_file.read_text() == "existing_config"
    assert script_file.read_text() == "existing_script"

def test_init_cmd_overwrites_existing_files_with_force(tmp_path):
    config_file = tmp_path / ".hamilton.toml"
    config_file.write_text("existing_config")
    
    script_dir = tmp_path / "tests"
    script_dir.mkdir()
    script_file = script_dir / "p1_validation.js"
    script_file.write_text("existing_script")
    
    init_cmd(tmp_path, force=True)
    
    assert config_file.read_text() != "existing_config"
    assert script_file.read_text() != "existing_script"
    assert "k6/http" in script_file.read_text()

def test_init_cmd_fails_if_path_not_dir(tmp_path):
    file_path = tmp_path / "not_a_dir"
    file_path.write_text("")
    
    with pytest.raises(SystemExit):
        init_cmd(file_path)


def test_init_cmd_subfolder_dockerfile_uses_posix_path(tmp_path):
    """
    VERIFY: If a Dockerfile is found in a subfolder, its path in the TOML
    uses forward slashes (/) even on Windows.

    Prevents TOMLDecodeError due to unescaped backslashes.
    """
    subfolder = tmp_path / "backend"
    subfolder.mkdir()
    (subfolder / "package.json").write_text("{}")
    (subfolder / "Dockerfile").write_text("FROM node:18\n")

    # Use programmatic=True to skip the prompt if multiple (though here only 1)
    init_cmd(tmp_path, programmatic=True)

    config_file = tmp_path / ".hamilton.toml"
    content = config_file.read_text()
    
    # Check that it uses forward slash
    assert 'dockerfile = "backend/Dockerfile"' in content
    # Ensure no backslashes made it into the string value
    assert 'backend\\Dockerfile' not in content
