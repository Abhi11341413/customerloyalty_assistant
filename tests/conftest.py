"""Shared fixtures: config, the built DB (read-only), a temp sample DB, a frozen `today`."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.config import policy

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "processed" / "loyalty.db"


@pytest.fixture(scope="session")
def pol():
    return policy()


@pytest.fixture(scope="session")
def today(pol):
    return pol.reference_date


@pytest.fixture(scope="session")
def db_path():
    if not DB.exists():
        pytest.skip("data/processed/loyalty.db missing - run: python src/data/build_tables.py --seed 42")
    return DB


@pytest.fixture(scope="session")
def con(db_path):
    from src.profile.assembler import connect
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture(scope="session")
def all_profiles(con):
    from src.profile.assembler import assemble_many
    return assemble_many(None, con=con)


@pytest.fixture()
def work_db(db_path, tmp_path):
    """A writable copy of the DB in a temp folder (tests never write to the real one)."""
    dst = tmp_path / "loyalty.db"
    shutil.copy(db_path, dst)
    return dst
