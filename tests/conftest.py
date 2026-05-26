import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Path to a fresh SQLite file under a temp dir."""
    return tmp_path / "state.db"


@pytest.fixture
def migrations_dir() -> Path:
    return Path(__file__).parent.parent / "migrations"
