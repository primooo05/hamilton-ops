import pytest
from core.exceptions import QualityViolation
from drivers.linter_driver import LinterDriver

def test_map_exit_code_with_empty_stdout_includes_stderr_summary():
    """
    VERIFY: If the linter fails (exit 1) but stdout is empty, the exception
    message should pull from the first line of stderr.
    """
    driver = LinterDriver(stage_path=".")
    stderr = "flake8: error: invalid choice: 'unknown_plugin'\nUsage: flake8 [options] file file ..."
    
    with pytest.raises(QualityViolation) as excinfo:
        driver._map_exit_code(1, stdout="", stderr=stderr)
    
    assert "Linter failed (exit 1): flake8: error: invalid choice: 'unknown_plugin'" in str(excinfo.value)
    assert excinfo.value.context["violations"] == 0

def test_map_exit_code_with_violations_in_stdout_uses_standard_message():
    """
    VERIFY: If stdout has content, it still uses the 'detected X violations' format.
    """
    driver = LinterDriver(stage_path=".")
    stdout = "file.py:1:1: E101 indentation contains mixed spaces and tabs\nfile.py:2:1: E101 ..."
    
    with pytest.raises(QualityViolation) as excinfo:
        driver._map_exit_code(1, stdout=stdout, stderr="")
    
    assert "Linter detected 2 violation(s) in staging area." in str(excinfo.value)
    assert excinfo.value.context["violations"] == 2
