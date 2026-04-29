from unittest.mock import patch

from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()

def test_ship_displays_banner(tmp_path, monkeypatch):
    """
    Contract: hamilton ship must display the welcome panel immediately.
    """
    monkeypatch.chdir(tmp_path)
    # Even if it fails doctor check, banner should show
    # Use -p to avoid hanging on prompt in tests
    result = runner.invoke(app, ["ship", "-p"])
    assert "Welcome to Hamilton-Ops" in result.output
    assert "v0.1" in result.output

def test_ship_blocked_without_doctor(tmp_path, monkeypatch):
    """
    Contract: hamilton ship must exit code 1 if .hamilton_doctor
    doesn't exist or shows status=fail.
    """
    monkeypatch.chdir(tmp_path)  # no .hamilton_doctor file here
    result = runner.invoke(app, ["ship"])
    assert result.exit_code == 1
    assert "hamilton doctor" in result.output.lower()

def test_ship_allowed_after_doctor_passes_programmatic(tmp_path, monkeypatch):
    """
    Contract: ship proceeds when .hamilton_doctor contains status=pass in programmatic mode.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".hamilton_doctor").write_text("status=pass\nstrategy=full\nram_gb=16.0\n")

    # Mock ship_cmd so we don't actually build anything
    with patch("cli.main.ship_cmd") as mock_ship:
        result = runner.invoke(app, ["ship", "--programmatic"])
    assert result.exit_code == 0
    mock_ship.assert_called_once()

def test_ship_aborted_by_user(tmp_path, monkeypatch):
    """
    Contract: ship exits gracefully if user rejects confirmation.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".hamilton_doctor").write_text("status=pass\nstrategy=full\nram_gb=16.0\n")

    with patch("cli.main.Confirm.ask", return_value=False):
        result = runner.invoke(app, ["ship"])
    
    assert result.exit_code == 0
    assert "Aborted by user" in result.output

def test_audit_blocked_without_doctor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["audit", "--artifact", "app.bin"])
    assert result.exit_code == 1