"""
Hamilton-Ops Configuration Templates
======================================

Contains the default `.hamilton.toml` structures and k6 validation scripts
for various language ecosystems. Used by `hamilton init` to scaffold new projects.
"""


_BASE_TOML = """[project]
name = "{name}"
image_tag = "{name}:latest"
dockerfile = "Dockerfile"
k6_script = "tests/p1_validation.js"

[quality]
linter_cmd = {linter_cmd}

[validation]
p95_ms = {p95_ms}
p99_ms = {p99_ms}
error_rate_percent = {error_rate}

[construction]
memory_gb = 4
"""

TEMPLATES = {
    "node": _BASE_TOML.format(
        name="{name}",
        linter_cmd='["npm", "run", "lint"]',
        p95_ms=250,
        p99_ms=600,
        error_rate=1.0,
    ),
    "python": _BASE_TOML.format(
        name="{name}",
        linter_cmd='["flake8", "."]',
        p95_ms=200,
        p99_ms=500,
        error_rate=1.0,
    ),
    "rust": _BASE_TOML.format(
        name="{name}",
        linter_cmd='["cargo", "clippy", "--", "-D", "warnings"]',
        p95_ms=100,
        p99_ms=250,
        error_rate=0.5,
    ),
    "go": _BASE_TOML.format(
        name="{name}",
        linter_cmd='["golangci-lint", "run"]',
        p95_ms=100,
        p99_ms=250,
        error_rate=0.5,
    ),
    "generic": _BASE_TOML.format(
        name="{name}",
        linter_cmd='["echo", "No linter configured"]',
        p95_ms=200,
        p99_ms=500,
        error_rate=1.0,
    ),
}


K6_SCRIPT_TEMPLATE = """import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  vus: 10,
  duration: '5s',
};

export default function () {
  // TARGET is injected by Hamilton-Ops during the P1 validation stream
  const target = __ENV.TARGET || 'http://localhost';
  
  // Note: Adjust this endpoint to match your application's health check or main route
  const res = http.get(`${target}/`);
  
  check(res, {
    'status is 200': (r) => r.status === 200,
  });
  
  sleep(1);
}
"""
