"""pytest fixtures for CORE HTTP Bridge tests."""
from __future__ import annotations
from contextlib import asynccontextmanager
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    import core_adapter
    class Identities(dict):
        def get(self,key,default=None): return "test-identity:"+str(key)
    monkeypatch.setattr(core_adapter,"identities",lambda bridge: Identities())
    import platform_db
    import target_admin
    from settings import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "temp_dir", tmp_path / "tmp")
    monkeypatch.setattr(platform_db, "DB_PATH", str(tmp_path / "platform.db"))
    monkeypatch.setattr(platform_db, "_initialized", False)
    monkeypatch.setattr(platform_db, "subscribe_events", lambda sid: None)
    for attr, name in {"MANIFEST_STORE": "manifests.json", "INSTANCE_STORE": "instances.json",
                       "TOPOLOGY_STORE": "topologies.json", "AUDIT_LOG": "audit.jsonl",
                       "IMAGE_IMPORT_DIR": "imports", "CUSTOM_SERVICES_DIR": "custom_services"}.items():
        monkeypatch.setattr(target_admin, attr, str(tmp_path / name))
    monkeypatch.setattr(target_admin, "_svc_cache", {"loaded": False, "names": set(), "registry": {}})


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def mock_bridge():
    return MagicMock()


@pytest.fixture
def client(mock_bridge: MagicMock, monkeypatch):
    import app as _app_module
    monkeypatch.setattr(_app_module.app.router, "lifespan_context", _noop_lifespan)
    monkeypatch.setattr(_app_module, "bridge", mock_bridge)
    with TestClient(_app_module.app) as c:
        yield c
