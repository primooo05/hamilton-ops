import pytest
from core.exceptions import ThresholdExceededError
from drivers.k6_driver import K6Driver

def test_check_thresholds_adds_unreachable_hint_on_100_percent_error_rate():
    """
    VERIFY: If the error rate is exactly 100.0%, the exception message should
    include a hint that the target might be unreachable.
    """
    driver = K6Driver(script_path="load.js", target="http://localhost:8080")
    metrics = {"p95_ms": 50.0, "p99_ms": 100.0, "error_rate": 100.0}
    
    with pytest.raises(ThresholdExceededError) as excinfo:
        driver._check_thresholds(metrics)
    
    assert "Error rate 100.00% exceeds threshold 1.0%" in str(excinfo.value)
    assert "(Target 'http://localhost:8080' may be unreachable)" in str(excinfo.value)

def test_check_thresholds_no_hint_on_partial_error_rate():
    """
    VERIFY: A 50% error rate is a failure but does not trigger the 'unreachable' hint.
    """
    driver = K6Driver(script_path="load.js", target="http://localhost:8080")
    metrics = {"p95_ms": 50.0, "p99_ms": 100.0, "error_rate": 50.0}
    
    with pytest.raises(ThresholdExceededError) as excinfo:
        driver._check_thresholds(metrics)
    
    assert "Error rate 50.00% exceeds threshold 1.0%" in str(excinfo.value)
    assert "may be unreachable" not in str(excinfo.value)
