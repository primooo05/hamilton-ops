import pytest
import sys
from core.stage import StagingContext
from core.exceptions import StagingError

@pytest.mark.asyncio
async def test_staging_immutability_contract(tmp_path):
    # Setup: Create a fake project
    source = tmp_path / "project"
    source.mkdir()
    (source / "app.py").write_text("print('hello')")

    async with StagingContext(source) as stage:
        assert stage.exists()
        assert (stage / "app.py").exists()
        # Contract: The stage path should be inside .hamilton/stage
        assert ".hamilton" in str(stage)

    # Contract: Stage must be cleaned up after exit
    assert not stage.exists()

@pytest.mark.asyncio
async def test_stage_excludes_ignored_directories(tmp_path):
    """
    Contract: .git, node_modules, .hamilton, target must never
    be copied into the stage — these would poison the build cache.
    """
    source = tmp_path / "project"
    source.mkdir()
    (source / ".git").mkdir()
    (source / "node_modules").mkdir()
    (source / "app.py").write_text("print('hello')")

    async with StagingContext(source) as stage:
        assert not (stage / ".git").exists()
        assert not (stage / "node_modules").exists()
        assert (stage / "app.py").exists()

@pytest.mark.asyncio
async def test_staging_raises_on_invalid_source(tmp_path):
    """
    Contract: StagingError must be raised if source does not exist.
    Supervisor depends on this to abort early before wasting resources.
    """
    fake_source = tmp_path / "nonexistent"

    with pytest.raises(StagingError):
        async with StagingContext(fake_source):
            pass

@pytest.mark.asyncio
async def test_cleanup_runs_even_on_exception(tmp_path):
    """
    Contract: Stage must be cleaned up even if the build crashes mid-flight.
    Zombie artifacts must not persist between runs.
    """
    source = tmp_path / "project"
    source.mkdir()
    (source / "app.py").write_text("print('hello')")

    with pytest.raises(RuntimeError):
        async with StagingContext(source) as stage:
            assert stage.exists()
            raise RuntimeError("Simulated build crash")

    # Stage must still be gone despite the crash
    assert not stage.exists()

@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="Symlink creation requires elevated privileges on Windows")
async def test_stage_does_not_contain_symlinks(tmp_path):
    """
    Contract: symlinks=False means no symlinks in the stage.
    Symlinks in a build context are a security boundary violation.
    """
    source = tmp_path / "project"
    source.mkdir()
    real_file = tmp_path / "secret.env"
    real_file.write_text("SECRET=123")
    (source / "link.env").symlink_to(real_file)

    async with StagingContext(source) as stage:
        staged_file = stage / "link.env"
        assert staged_file.exists()
        assert not staged_file.is_symlink()