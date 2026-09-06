from __future__ import annotations

import os
from pathlib import Path

TEST_DB = Path(__file__).resolve().parent / "test_travel_core.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ["API_KEY"] = "test-key"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from travel_core.database import Base, engine  # noqa: E402
from travel_core.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


@pytest.fixture
def headers():
    return {
        "X-API-Key": "test-key",
        "X-Tenant-ID": "training",
        "X-User-ID": "learner-1",
        "X-Correlation-ID": "test-run-1",
    }
