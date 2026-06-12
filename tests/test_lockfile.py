import pytest

from rabbit_hole.lockfile import PipelineLockError, acquire_lock


def test_second_acquire_fails_while_held(tmp_path):
    lock = acquire_lock(tmp_path / "run.lock")
    with pytest.raises(PipelineLockError):
        acquire_lock(tmp_path / "run.lock")
    lock.close()


def test_lock_reacquirable_after_release(tmp_path):
    acquire_lock(tmp_path / "run.lock").close()
    lock = acquire_lock(tmp_path / "run.lock")  # must not raise
    lock.close()
