"""Pytest fixtures: real PostgreSQL via pgserver + FastAPI test client.

Env must be configured before ``app.*`` is imported, so this conftest sets
``DATABASE_URL`` at module top level (pytest imports conftest first).
"""

from __future__ import annotations

import os
import tempfile

import pgserver
import pytest

_PG_DIR = tempfile.mkdtemp(prefix="recycle-pg-")
_server = pgserver.get_server(_PG_DIR, cleanup_mode="stop")
os.environ["DATABASE_URL"] = _server.get_uri()

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:  # lifespan runs init_db() against pgserver
        yield c


def pytest_sessionfinish(session, exitstatus):  # pragma: no cover
    try:
        _server.cleanup()
    except Exception:
        pass
