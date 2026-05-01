"""
Unit Tests — cli/discovery.py
==============================

Tests the DiscoveryEngine against synthetic directory structures that
simulate the most common real-world codebase layouts:

    - Simple monolith (single ecosystem, one Dockerfile)
    - Monorepo with multiple distinct services
    - Project with nested build output / blacklisted dirs
    - Empty project (no fingerprints)

Naming convention:
    test_<subject>_<condition>_<expected_outcome>
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.discovery import BLACKLIST_DIRS, DiscoveryEngine, _classify_dir



def _make_file(path: Path, content: str = "") -> None:
    """Create a file and all parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_classify_dir_detects_node(tmp_path):
    _make_file(tmp_path / "package.json", "{}")
    unit = _classify_dir(tmp_path, depth=0)
    assert unit is not None
    assert unit.ecosystem == "node"


def test_classify_dir_detects_python_pyproject(tmp_path):
    _make_file(tmp_path / "pyproject.toml", "")
    unit = _classify_dir(tmp_path, depth=0)
    assert unit is not None
    assert unit.ecosystem == "python"


def test_classify_dir_detects_rust(tmp_path):
    _make_file(tmp_path / "Cargo.toml", "")
    unit = _classify_dir(tmp_path, depth=0)
    assert unit is not None
    assert unit.ecosystem == "rust"


def test_classify_dir_detects_go(tmp_path):
    _make_file(tmp_path / "go.mod", "")
    unit = _classify_dir(tmp_path, depth=0)
    assert unit is not None
    assert unit.ecosystem == "go"


def test_classify_dir_detects_dockerfile_only_as_generic(tmp_path):
    """A directory with ONLY a Dockerfile and no manifest is still a buildable unit."""
    _make_file(tmp_path / "Dockerfile", "FROM scratch\n")
    unit = _classify_dir(tmp_path, depth=0)
    assert unit is not None
    assert unit.ecosystem == "generic"


def test_classify_dir_returns_none_for_empty_dir(tmp_path):
    """A completely empty directory should not be classified."""
    unit = _classify_dir(tmp_path, depth=0)
    assert unit is None


def test_classify_dir_records_dockerfile_path(tmp_path):
    """When a Dockerfile is present alongside a manifest, it's recorded."""
    _make_file(tmp_path / "package.json", "{}")
    _make_file(tmp_path / "Dockerfile", "FROM node:18\n")
    unit = _classify_dir(tmp_path, depth=0)
    assert unit is not None
    assert unit.dockerfile == tmp_path / "Dockerfile"


def test_classify_dir_dockerfile_none_when_absent(tmp_path):
    """If there is a manifest but no Dockerfile, dockerfile is None."""
    _make_file(tmp_path / "package.json", "{}")
    unit = _classify_dir(tmp_path, depth=0)
    assert unit is not None
    assert unit.dockerfile is None



def test_discovery_simple_monolith(tmp_path):
    """
    VERIFY: A simple project at root level is detected as a single unit.
    """
    _make_file(tmp_path / "package.json", "{}")
    _make_file(tmp_path / "Dockerfile", "FROM node:18\n")

    units = DiscoveryEngine(workspace=tmp_path).scan()

    assert len(units) == 1
    assert units[0].ecosystem == "node"
    assert units[0].depth == 0


def test_discovery_monorepo_finds_all_services(tmp_path):
    """
    VERIFY: A monorepo with distinct service sub-directories is fully indexed.
    
    Simulates a structure like Machete_Root:
        machete-backend/  (node + Dockerfile)
        machete-frontend/ (node + Dockerfile)
        infra/            (python)
    """
    _make_file(tmp_path / "machete-backend"  / "package.json", "{}")
    _make_file(tmp_path / "machete-backend"  / "Dockerfile",   "FROM node:18\n")
    _make_file(tmp_path / "machete-frontend" / "package.json", "{}")
    _make_file(tmp_path / "machete-frontend" / "Dockerfile",   "FROM node:18\n")
    _make_file(tmp_path / "infra"            / "pyproject.toml", "")

    units = DiscoveryEngine(workspace=tmp_path).scan()
    names = {u.name for u in units}

    assert "machete-backend"  in names
    assert "machete-frontend" in names
    assert "infra"            in names


def test_discovery_does_not_enter_blacklisted_dirs(tmp_path):
    """
    VERIFY: The blacklist prevents descending into node_modules, .git, venv, etc.
    
    A fingerprint file placed inside a blacklisted dir must NOT produce a unit.
    """
    for blacklisted in ["node_modules", ".git", "venv", "dist", "__pycache__"]:
        _make_file(tmp_path / blacklisted / "package.json", "{}")

    units = DiscoveryEngine(workspace=tmp_path).scan()

    # None of the blacklisted dirs should appear as units.
    assert len(units) == 0


def test_discovery_depth_cap_prevents_deep_traversal(tmp_path):
    """
    VERIFY: Directories beyond max_depth are not classified.
    
    Creates a fingerprint 5 levels deep; DiscoveryEngine with max_depth=4
    should not find it.
    """
    deep_dir = tmp_path / "a" / "b" / "c" / "d" / "e"
    _make_file(deep_dir / "package.json", "{}")

    units = DiscoveryEngine(workspace=tmp_path, max_depth=4).scan()

    # The file is at depth 5 which is beyond max_depth=4 — should not appear.
    names = {u.name for u in units}
    assert "e" not in names


def test_discovery_returns_empty_list_for_no_fingerprints(tmp_path):
    """
    VERIFY: A directory with no known fingerprints returns an empty list.
    """
    _make_file(tmp_path / "README.md", "# My Project")
    _make_file(tmp_path / "main.c", "int main() { return 0; }")

    units = DiscoveryEngine(workspace=tmp_path).scan()

    assert units == []


def test_discovery_sort_order_is_depth_then_name(tmp_path):
    """
    VERIFY: Results are sorted shallowest first, then alphabetically.
    """
    _make_file(tmp_path / "b-service" / "package.json", "{}")
    _make_file(tmp_path / "a-service" / "package.json", "{}")
    # Also create a node at depth 0 (the root itself has no manifest — skipped)

    units = DiscoveryEngine(workspace=tmp_path).scan()

    # Both are at depth 1; alphabetical order means a-service first.
    assert len(units) == 2
    assert units[0].name == "a-service"
    assert units[1].name == "b-service"


def test_discovery_find_dockerfiles_returns_all(tmp_path):
    """
    VERIFY: find_dockerfiles() walks the tree and returns every Dockerfile found.
    """
    _make_file(tmp_path / "backend"  / "Dockerfile", "FROM node:18\n")
    _make_file(tmp_path / "frontend" / "Dockerfile", "FROM node:18\n")
    _make_file(tmp_path / "node_modules" / "some_pkg" / "Dockerfile", "FROM scratch\n")

    engine = DiscoveryEngine(workspace=tmp_path)
    dockerfiles = engine.find_dockerfiles()

    # Should find backend and frontend Dockerfiles, but NOT the one in node_modules.
    paths = {str(d.parent.name) for d in dockerfiles}
    assert "backend"  in paths
    assert "frontend" in paths
    assert "node_modules" not in paths
    assert "some_pkg"     not in paths


def test_discovery_microservice_with_mixed_ecosystems(tmp_path):
    """
    VERIFY: A polyrepo with mixed ecosystems (Node + Python + Go) is fully indexed
    with correct ecosystem tags per service.
    """
    _make_file(tmp_path / "api-gateway" / "go.mod",        "module gateway\n")
    _make_file(tmp_path / "api-gateway" / "Dockerfile",    "FROM golang:1.22\n")
    _make_file(tmp_path / "web-app"     / "package.json",  "{}")
    _make_file(tmp_path / "web-app"     / "Dockerfile",    "FROM node:20\n")
    _make_file(tmp_path / "ml-service"  / "requirements.txt", "torch\n")

    units = DiscoveryEngine(workspace=tmp_path).scan()
    by_name = {u.name: u for u in units}

    assert by_name["api-gateway"].ecosystem  == "go"
    assert by_name["web-app"].ecosystem      == "node"
    assert by_name["ml-service"].ecosystem   == "python"


def test_discovery_blacklist_is_complete_set():
    """
    VERIFY: BLACKLIST_DIRS contains the directories we depend on being excluded.
    
    This is a contract test: if someone removes an entry from the blacklist,
    this test catches it before it causes a perf regression on real projects.
    """
    required = {"node_modules", ".git", "venv", ".venv", "dist", "__pycache__", "target"}
    assert required.issubset(BLACKLIST_DIRS)
