"""
Hamilton-Ops Configuration System
===================================

Provides two public utilities:

    load_hamilton_config(path)
        Parse ``.hamilton.toml`` and return its contents as a plain dict.
        Uses the stdlib ``tomllib`` (Python 3.11+). The file is optional —
        if it does not exist the function returns an empty dict so callers
        can always use dict.get() without guarding against None.

    compute_project_hash(stage_path)
        Compute a deterministic SHA-256 fingerprint of the project's
        dependency lockfiles. This hash is injected as ``--build-arg
        CACHE_ID=<hash>`` into the docker build command, scoping the
        BuildKit layer cache per unique dependency set. Without this
        scoping, a shared CI runner that builds project A and then project
        B may serve stale cached layers across project boundaries.

        Lockfiles discovered (in order, first found wins per ecosystem):
            Node.js  — package-lock.json, yarn.lock, pnpm-lock.yaml
            Python   — poetry.lock, requirements.txt, Pipfile.lock
            Rust     — Cargo.lock
            Go       — go.sum
            Java     — pom.xml (fallback for Maven projects without lockfiles)

        If no lockfile is found, returns an empty string so the construction
        driver skips the ``--build-arg CACHE_ID`` injection entirely.

Configuration schema (all keys optional — defaults are baked in):

    [project]
    name        = "my-app"          # logical name for forensic reports
    k6_script   = "tests/load.js"  # path to the k6 JS test, relative to stage
    image_tag   = "myapp:latest"    # Docker image tag
    cache_ref   = "ghcr.io/..."     # BuildKit registry cache reference

    [validation]
    p95_ms              = 200       # P95 latency threshold in milliseconds
    p99_ms              = 500       # P99 latency threshold in milliseconds
    error_rate_percent  = 1.0       # maximum acceptable HTTP error rate

    [construction]
    memory_gb = 4                   # Docker build memory cap (GB)

Design rationale:
    - ``tomllib`` is in the Python 3.11+ stdlib and requires no extra dep.
    - On Python 3.10, ``tomli`` (the backport) is used if installed; otherwise
      the function raises an ImportError with a clear install message.
    - Hashing is order-independent within a file but file-order-dependent:
      we sort lockfile paths before hashing so the result is reproducible
      across OS and filesystem enumeration order differences.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hamilton.config")


_LOCKFILE_CANDIDATES: list[str] = [
    # Node.js
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    # Python
    "poetry.lock",
    "requirements.txt",
    "Pipfile.lock",
    # Rust
    "Cargo.lock",
    # Go
    "go.sum",
    # Java / Maven (fallback — pom.xml changes correlate with dependency changes)
    "pom.xml",
]


def load_hamilton_config(config_path: str | Path) -> dict:
    """
    Parse the ``.hamilton.toml`` file and return its contents as a dict.

    If the file does not exist at ``config_path``, an empty dict is returned
    (not an error) so that ``ship_cmd`` can use ``dict.get()`` for all keys
    without special-casing a missing config.

    Args:
        config_path: Path to ``.hamilton.toml``. Typically the project root.

    Returns:
        Parsed TOML contents as a nested dict, or ``{}`` if the file is absent.

    Raises:
        ImportError: If Python < 3.11 and ``tomli`` is not installed.
        ValueError:  If the TOML file cannot be parsed (syntax error).
    """
    path = Path(config_path)

    # Handle both "path to directory" and "path to file" callers.
    if path.is_dir():
        path = path / ".hamilton.toml"

    if not path.exists():
        logger.debug("CONFIG: No .hamilton.toml found at %s — using defaults.", path)
        return {}

    # Resolve TOML parser — stdlib on 3.11+, tomli backport on 3.10.
    if sys.version_info >= (3, 11):
        import tomllib  # stdlib
        open_mode = "rb"
    else:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
            open_mode = "rb"
        except ImportError:
            raise ImportError(
                "Python < 3.11 detected. Install 'tomli' to parse .hamilton.toml: "
                "  pip install tomli"
            )

    try:
        with open(path, open_mode) as fh:
            config = tomllib.load(fh)
        logger.info("CONFIG: Loaded .hamilton.toml from %s", path)
        return config
    except Exception as exc:
        raise ValueError(
            f"Failed to parse .hamilton.toml at '{path}': {exc}"
        ) from exc


def compute_project_hash(stage_path: str | Path) -> str:
    """
    Compute a deterministic SHA-256 fingerprint of the project's lockfiles.

    The hash is injected as ``--build-arg CACHE_ID=<hash>`` into the
    ``docker build`` command so BuildKit's layer cache is namespaced per
    unique dependency state. This prevents stale cache hits on shared CI
    runners that build multiple projects.

    Algorithm:
        1. Enumerate all known lockfile candidates under ``stage_path``.
        2. Sort the discovered paths for deterministic ordering.
        3. Hash each file's contents into a single running SHA-256 digest,
           prefixing each file with its relative path so that two projects
           with identical contents but different lockfile names produce
           different hashes.
        4. Return the first 16 hex characters of the final digest.
           (16 chars = 64 bits of collision resistance — sufficient for
           cache-key namespacing, far beyond what UUID v4 offers.)

    Args:
        stage_path: Root of the staging directory (or project root).

    Returns:
        A 16-character hex string, or ``""`` if no lockfile was found.
        An empty return means the ConstructionDriver will omit ``CACHE_ID``
        entirely rather than injecting a meaningless value.
    """
    stage = Path(stage_path)
    hasher = hashlib.sha256()
    found_any = False

    # Collect all matching lockfiles, sort for determinism.
    discovered: list[Path] = sorted(
        p for name in _LOCKFILE_CANDIDATES
        for p in stage.rglob(name)
        if p.is_file()
    )

    for lockfile in discovered:
        # Include the relative path in the hash so that a project with
        # poetry.lock and requirements.txt in different subdirs produces a
        # different hash than one with only requirements.txt in the root.
        rel = lockfile.relative_to(stage)
        hasher.update(str(rel).encode())
        hasher.update(lockfile.read_bytes())
        found_any = True
        logger.debug("CONFIG: Hashed lockfile %s", rel)

    if not found_any:
        logger.warning(
            "CONFIG: No lockfile found under %s — CACHE_ID will not be injected. "
            "BuildKit layer caching will not be namespaced per project.",
            stage,
        )
        return ""

    digest = hasher.hexdigest()[:16]
    logger.info("CONFIG: Computed project_hash=%s (from %d lockfile(s))", digest, len(discovered))
    return digest
