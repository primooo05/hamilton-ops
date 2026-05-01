"""
Hamilton-Ops Project Discovery Engine
=======================================

Scans a project directory in parallel to identify buildable components
and their ecosystems. Used by ``hamilton init`` to auto-configure TOML
for monorepos, microservice layouts, polyrepos, and monoliths.

Design
------
``DiscoveryEngine.scan()`` uses a ``ThreadPoolExecutor`` to walk multiple
subdirectories concurrently. Each walker returns a ``ProjectUnit`` if it
detects an ecosystem fingerprint. The parent thread collects results and
deduplicates by path.

Filesystem traversal rules:
    - Depth is capped at ``max_depth`` (default: 4) to prevent scanning
      deep virtual-env or dist trees.
    - ``BLACKLIST_DIRS`` are pruned *in-place* from ``os.walk`` so their
      contents are never visited — this is the key performance safety net
      for monorepos with huge ``node_modules``.
    - Only directories are classified as ``ProjectUnit`` objects. Plain
      files at the root level contribute to root-level detection only.

Ecosystem fingerprints (ordered by priority within a directory)
---------------------------------------------------------------
    node       package.json
    python     pyproject.toml | requirements.txt | Pipfile
    rust       Cargo.toml
    go         go.mod
    java       pom.xml | build.gradle
    generic    Dockerfile only (no known manifest)
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hamilton.cli.discovery")

# Directories that are never traversed — pruned before descent.
BLACKLIST_DIRS: frozenset[str] = frozenset([
    "node_modules",
    ".git",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "venv",
    ".venv",
    "env",
    ".env",
    "dist",
    "build",
    "target",       # Rust / Maven
    ".next",        # Next.js build output
    ".nuxt",        # Nuxt.js build output
    "out",          # generic build output
    "coverage",
    ".idea",
    ".vscode",
    "__snapshots__",
])

# Maps filename → ecosystem tag.  Order determines priority within a dir.
_FINGERPRINTS: list[tuple[str, str]] = [
    ("package.json",      "node"),
    ("pyproject.toml",    "python"),
    ("requirements.txt",  "python"),
    ("Pipfile",           "python"),
    ("Cargo.toml",        "rust"),
    ("go.mod",            "go"),
    ("pom.xml",           "java"),
    ("build.gradle",      "java"),
    ("build.gradle.kts",  "java"),
]


@dataclass(frozen=True)
class ProjectUnit:
    """
    A single buildable component detected during discovery.

    Attributes:
        name:       Human-readable component name (directory name).
        root:       Absolute path to the component's root directory.
        ecosystem:  Language/runtime ecosystem tag (node, python, rust, ...).
        dockerfile: Absolute path to the Dockerfile if one exists in root,
                    or None if no Dockerfile was found.
        depth:      Directory depth from the workspace root (0 = workspace root).
    """
    name: str
    root: Path
    ecosystem: str
    dockerfile: Optional[Path]
    depth: int


def _classify_dir(path: Path, depth: int) -> Optional[ProjectUnit]:
    """
    Inspect a single directory and return a ``ProjectUnit`` if it has a
    known ecosystem fingerprint, otherwise ``None``.

    A directory qualifies as a ProjectUnit if:
        1. It contains at least one ecosystem fingerprint file, OR
        2. It contains a Dockerfile (tagged "generic").

    Args:
        path:  Absolute path to the directory.
        depth: Depth relative to the workspace root.

    Returns:
        A ``ProjectUnit`` or ``None``.
    """
    try:
        entries = {e.name for e in path.iterdir() if e.is_file()}
    except PermissionError:
        logger.debug("DISCOVERY: Permission denied — skipping %s", path)
        return None

    ecosystem = None
    for filename, tag in _FINGERPRINTS:
        if filename in entries:
            ecosystem = tag
            break

    # A directory with only a Dockerfile is a generic buildable unit.
    if ecosystem is None and "Dockerfile" in entries:
        ecosystem = "generic"

    if ecosystem is None:
        return None

    dockerfile = path / "Dockerfile" if "Dockerfile" in entries else None

    return ProjectUnit(
        name=path.name,
        root=path,
        ecosystem=ecosystem,
        dockerfile=dockerfile,
        depth=depth,
    )


class DiscoveryEngine:
    """
    Parallel project discovery for complex codebases.

    Walks the workspace directory up to ``max_depth`` levels deep, using a
    ``ThreadPoolExecutor`` to classify subdirectories concurrently. Returns a
    ranked list of ``ProjectUnit`` objects — sorted by depth (shallowest first)
    then alphabetically by name for deterministic ordering.

    Usage::

        engine = DiscoveryEngine(workspace=Path("/projects/machete"))
        units = engine.scan()
        # → [ProjectUnit("machete-backend", ...), ProjectUnit("machete-frontend", ...)]

    Thread safety:
        ``_classify_dir`` is stateless and reads only the filesystem, so it is
        safe to call concurrently from the ThreadPoolExecutor workers.
    """

    def __init__(
        self,
        workspace: Path,
        max_depth: int = 4,
        max_workers: int = 8,
    ) -> None:
        """
        Args:
            workspace:    Root directory to scan.
            max_depth:    Maximum depth to descend (0 = root only, 4 = default).
            max_workers:  Number of parallel threads for directory classification.
        """
        self._workspace = workspace.resolve()
        self._max_depth = max_depth
        self._max_workers = max_workers

    def scan(self) -> list[ProjectUnit]:
        """
        Execute the parallel discovery scan.

        Returns:
            List of ``ProjectUnit`` objects sorted by (depth, name).
            The workspace root itself is always evaluated first (depth=0).
        """
        dirs_to_classify: list[tuple[Path, int]] = []

        # Collect all candidate directories using os.walk with blacklist pruning.
        # Pruning dirnames in-place prevents os.walk from descending into them.
        for dirpath, dirnames, _ in os.walk(self._workspace):
            current = Path(dirpath)
            depth = len(current.relative_to(self._workspace).parts)

            if depth > self._max_depth:
                # Prune: stop descending further.
                dirnames.clear()
                continue

            # Prune blacklisted dirs in-place — os.walk respects this.
            dirnames[:] = [
                d for d in dirnames
                if d not in BLACKLIST_DIRS and not d.startswith(".")
            ]

            dirs_to_classify.append((current, depth))

        logger.debug(
            "DISCOVERY: Collected %d candidate directories to classify.",
            len(dirs_to_classify),
        )

        # Classify directories in parallel.
        units: list[ProjectUnit] = []
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {
                executor.submit(_classify_dir, path, depth): (path, depth)
                for path, depth in dirs_to_classify
            }
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    units.append(result)

        # Sort: shallowest first, then alphabetical for determinism.
        units.sort(key=lambda u: (u.depth, u.name))

        logger.info(
            "DISCOVERY: Found %d buildable component(s) in '%s'.",
            len(units),
            self._workspace,
        )

        return units

    def find_dockerfiles(self) -> list[Path]:
        """
        Return a flat list of all Dockerfiles found in the workspace,
        recursively, respecting the blacklist. Useful for targeted queries
        where only the Dockerfile paths matter.
        """
        results: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self._workspace):
            dirnames[:] = [
                d for d in dirnames
                if d not in BLACKLIST_DIRS and not d.startswith(".")
            ]
            if "Dockerfile" in filenames:
                results.append(Path(dirpath) / "Dockerfile")
        return results
