from unittest.mock import patch

from cli.doctor import Doctor, HardwareProfile, ExecutionStrategy

def test_strategy_full_requires_4_cores_and_8gb():
    profile = HardwareProfile(cores=4, ram_gb=8.0, is_ssd=True)
    assert profile.strategy == ExecutionStrategy.FULL

def test_strategy_reduced_for_2_cores():
    profile = HardwareProfile(cores=2, ram_gb=16.0, is_ssd=True)
    assert profile.strategy == ExecutionStrategy.REDUCED

def test_strategy_reduced_for_low_ram():
    """
    Contract: 4 cores but only 6GB RAM → REDUCED, not FULL.
    Docker needs headroom.
    """
    profile = HardwareProfile(cores=4, ram_gb=6.0, is_ssd=True)
    assert profile.strategy == ExecutionStrategy.REDUCED

def test_strategy_minimal_for_single_core():
    profile = HardwareProfile(cores=1, ram_gb=4.0, is_ssd=False)
    assert profile.strategy == ExecutionStrategy.MINIMAL

def test_doctor_writes_state_file(tmp_path, monkeypatch):
    """
    Contract: run_diagnostics must write .hamilton_doctor with
    correct status so ship() can read it.
    """
    monkeypatch.chdir(tmp_path)

    doc = Doctor()
    # Mock all subprocess calls so we don't need real tools installed
    with patch("cli.doctor.subprocess.check_output", return_value=b"1.0.0"):
        with patch("cli.doctor.HardwareProfile.detect", return_value=
        HardwareProfile(cores=4, ram_gb=16.0, is_ssd=True)):
            doc.run_diagnostics(persist=True)

    state_file = tmp_path / ".hamilton_doctor"
    assert state_file.exists()
    content = state_file.read_text()
    assert "status=" in content
    assert "strategy=" in content

def test_doctor_writes_fail_status_when_errors_exist(tmp_path, monkeypatch):
    """
    Contract: If any tool is missing (error), status=fail must be written.
    ship() depends on this to enforce the doctor-first rule.
    """
    monkeypatch.chdir(tmp_path)
    doc = Doctor()

    with patch("cli.doctor.subprocess.check_output",
               side_effect=FileNotFoundError):  # all tools missing
        with patch("cli.doctor.HardwareProfile.detect", return_value=
        HardwareProfile(cores=4, ram_gb=16.0, is_ssd=True)):
            doc.run_diagnostics(persist=True)

    content = (tmp_path / ".hamilton_doctor").read_text()
    assert "status=fail" in content