"""
Hamilton Staging Engine

Handles the creation of immutable snapshots for the build context.
Ensures that the 'Construction' stream is decoupled from the 'Live' source
to prevent race conditions and dirty cache states.
"""
import asyncio
import shutil
from functools import partial
from pathlib import Path

from .exceptions import StagingError

class StagingContext:
    """
    Async context manager for managing the Hamilton staging lifecycle.
    Attributes:
        source_path (Path): The original repository root.
        stage_path (Path): The dedicated snapshot directory (.hamilton/stage).
    """
    def __init__(self, source_path: str | Path):
        self.source_path = Path(source_path).resolve()
        self.stage_path = self.source_path / ".hamilton" / "stage"
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Path:
        """
        Prepares the immutable snapshot.

        1. Validates source existence.
        2. Clears stale staging data.
        3. Executes a non-symlink copy of the source.
        """
        async with self._lock:
            if not self.source_path.is_dir():
                raise StagingError(f"Source path {self.source_path} does not exist or is not a directory")

            await self._clear_stage()

            try:
                await asyncio.to_thread(
                    partial(
                        shutil.copytree,
                        self.source_path,
                        self.stage_path,
                        symlinks=False,
                        ignore=shutil.ignore_patterns(".git", ".hamilton", "node_modules", "target"),
                        dirs_exist_ok=False,  # we just deleted it, fail fast if something recreates it
                    )
                )
                return self.stage_path
            except Exception as exc:
                # preserve original traceback
                raise StagingError(f"Failed to initialize stage at {self.stage_path}") from exc

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Ensures idempotent cleanup of the staging area.
        Reaps any zombie artifacts if the flight is aborted.
        """
        async with self._lock:
            await self._clear_stage()

    async def _clear_stage(self):
        """Internal helper to safely remove the staging directory."""
        if self.stage_path.exists():
            await asyncio.to_thread(partial(shutil.rmtree, self.stage_path))

    async def cleanup_zombies(self):
        """
        Emergency cleanup for orphan processes or files.
        Can be called by the Supervisor during a P1 Hamilton Alarm.
        """
        # Logic for deep cleanup (to be extended for Docker containers)
        await self._clear_stage()
