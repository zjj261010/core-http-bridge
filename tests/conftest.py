"""pytest fixtures for CORE HTTP Bridge tests."""
from __future__ import annotations
from contextlib import asynccontextmanager
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def mock_bridge():
    return MagicMock()


@pytest.fixture
def client(mock_bridge: MagicMock):
    import app as _app_module
    _app_module.app.router.lifespan_context = _noop_lifespan
    _app_module.bridge = mock_bridge
    with TestClient(_app_module.app) as c:
        yield c
