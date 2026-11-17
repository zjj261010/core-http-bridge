"""Unit tests for FastAPI routes in app.py (mock gRPC, test HTTP responses)."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from fastapi.testclient import TestClient

# conftest.py already patches CoreGrpcBridge globally
from conftest import client, mock_bridge  # noqa: F401

from core.api.grpc import wrappers


# =========================================================================
# Helper to create mock wrappers with realistic attributes
# =========================================================================

def _mock_session(id: int = 1, state: int = 1):
    s = MagicMock()
    s.id = id
    type(s).state = PropertyMock(return_value=MagicMock(value=state))
    s.nodes = {}
    s.links = []
    s.dir = f"/tmp/pycore.{id}"
    s.user = "root"
    s.file = None
    return s


def _mock_node(id: int = 1, name: str = "n1"):
    n = MagicMock()
    n.id = id
    n.name = name
    type(n).type = PropertyMock(return_value=MagicMock(value=0))
    n.model = "PC"
    n.position = MagicMock(x=100.0, y=200.0)
    n.geo = None
    n.services = {"DefaultRoute"}
    n.emane = ""
    n.icon = ""
    n.image = ""
    n.server = ""
    n.dir = ""
    n.channel = ""
    n.canvas = 0
    return n


def _mock_iface():
    i = MagicMock()
    i.id = 0
    i.name = "eth0"
    i.ip4 = "10.0.0.1"
    i.ip4_mask = 24
    i.ip6 = ""
    i.ip6_mask = 0
    i.mac = "00:00:00:00:00:01"
    i.mtu = 1500
    i.node_id = 0
    return i


def _mock_link():
    l = MagicMock()
    l.node1_id = 1
    l.node2_id = 2
    type(l).type = PropertyMock(return_value=MagicMock(value=1))
    l.iface1 = _mock_iface()
    l.iface2 = _mock_iface()
    l.options = None
    return l


def _mock_config_option():
    co = MagicMock()
    co.label = "Range"
    co.name = "range"
    co.value = "300"
    co.type = 7
    co.select = []
    co.group = "Basic"
    return co


# =========================================================================
# Health
# =========================================================================

class TestHealth:
    def test_ok(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_sessions.return_value = []
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["sessions"] == 0

    def test_bridge_unavailable(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_sessions.side_effect = RuntimeError("connection lost")
        resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert "unavailable" in data["error"]


# =========================================================================
# Session Routes
# =========================================================================

class TestSessionRoutes:
    def test_list(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_sessions.return_value = []
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_with_data(self, client: TestClient, mock_bridge: MagicMock):
        s = _mock_session(id=1, state=4)
        mock_bridge.get_sessions.return_value = [s]
        resp = client.get("/api/sessions")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == 1

    def test_create(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.create_session.return_value = _mock_session(id=5, state=1)
        resp = client.post("/api/sessions", json={})
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == 5

    def test_get(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_session.return_value = _mock_session(id=3, state=2)
        resp = client.get("/api/sessions/3")
        assert resp.status_code == 200
        assert resp.json()["id"] == 3

    def test_delete(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.delete_session.return_value = True
        resp = client.delete("/api/sessions/1")
        assert resp.status_code == 200
        assert resp.json() == {"result": True}

    def test_check_exists(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.check_session.return_value = True
        resp = client.get("/api/sessions/1/check")
        assert resp.json() == {"result": True}

    def test_check_not_exists(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.check_session.return_value = False
        resp = client.get("/api/sessions/999/check")
        assert resp.json() == {"result": False}

    def test_start(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_session.return_value = _mock_session(id=1)
        mock_bridge.start_session.return_value = (True, [])
        resp = client.post("/api/sessions/1/start", json={"definition": True})
        assert resp.json() == {"result": True, "exceptions": []}

    def test_stop(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.stop_session.return_value = True
        mock_bridge.start_session.return_value = (True, [])
        mock_bridge.get_session.return_value = _mock_session()
        resp = client.post("/api/sessions/1/stop")
        assert resp.json() == {"result": True}

    def test_alert(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.alert.return_value = True
        resp = client.post("/api/sessions/1/alert",
                          json={"level": "WARNING", "source": "t", "text": "m"})
        assert resp.json() == {"result": True}

    def test_alert_invalid_level(self, client: TestClient, mock_bridge: MagicMock):
        resp = client.post("/api/sessions/1/alert",
                          json={"level": "BAD", "source": "t", "text": "m"})
        assert resp.status_code == 400
        assert "invalid" in resp.json()["error"]


# =========================================================================
# Node Routes
# =========================================================================

class TestNodeRoutes:
    def test_add(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.add_node.return_value = 1
        resp = client.post(
            "/api/sessions/1/nodes",
            json={"id": 1, "name": "n1", "model": "PC",
                  "position": {"x": 100, "y": 100}},
        )
        assert resp.status_code == 201
        assert resp.json() == {"node_id": 1}

    def test_add_invalid_type(self, client: TestClient, mock_bridge: MagicMock):
        resp = client.post(
            "/api/sessions/1/nodes",
            json={"id": "abc", "name": "n1"},
        )
        assert resp.status_code == 422

    def test_get(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_node.return_value = (_mock_node(), [], [])
        resp = client.get("/api/sessions/1/nodes/1")
        assert resp.status_code == 200
        assert resp.json()["node"]["name"] == "n1"

    def test_edit(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.edit_node.return_value = True
        resp = client.patch("/api/sessions/1/nodes/1?icon=router.png")
        assert resp.json() == {"result": True}

    def test_delete(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.delete_node.return_value = True
        resp = client.delete("/api/sessions/1/nodes/1")
        assert resp.json() == {"result": True}

    def test_move_position(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.move_node.return_value = True
        resp = client.post(
            "/api/sessions/1/nodes/1/move",
            json={"position": {"x": 200, "y": 150}},
        )
        assert resp.json() == {"result": True}

    def test_move_missing(self, client: TestClient, mock_bridge: MagicMock):
        resp = client.post("/api/sessions/1/nodes/1/move", json={})
        assert resp.status_code == 400
        assert "must provide" in resp.json()["error"]

    def test_command(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.node_command.return_value = (0, "hello")
        resp = client.post(
            "/api/sessions/1/nodes/1/command",
            json={"command": "echo hello", "wait": True, "shell": True},
        )
        assert resp.json() == {"return_code": 0, "output": "hello"}

    def test_terminal(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_node_terminal.return_value = "vcmd ... /bin/bash"
        resp = client.get("/api/sessions/1/nodes/1/terminal")
        assert "terminal" in resp.json()


# =========================================================================
# Link Routes
# =========================================================================

class TestLinkRoutes:
    def test_add(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.add_link.return_value = (True, _mock_iface(), _mock_iface())
        resp = client.post(
            "/api/sessions/1/links",
            json={"node1_id": 1, "node2_id": 2,
                  "iface1": {"id": 0}, "iface2": {"id": 0}},
        )
        assert resp.status_code == 201
        assert resp.json()["result"] is True

    def test_edit(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.edit_link.return_value = True
        resp = client.patch(
            "/api/sessions/1/links",
            json={"node1_id": 1, "node2_id": 2,
                  "iface1": {"id": 0}, "iface2": {"id": 0},
                  "options": {"bandwidth": 10000000}},
        )
        assert resp.json() == {"result": True}

    def test_delete(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.delete_link.return_value = True
        import json as _json
        resp = client.request("DELETE",
            "/api/sessions/1/links",
            content=_json.dumps({"node1_id": 1, "node2_id": 2,
                    "iface1": {"id": 0}, "iface2": {"id": 0}}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.json() == {"result": True}

    def test_linked_false(self, client: TestClient, mock_bridge: MagicMock):
        resp = client.post(
            "/api/sessions/1/links/linked",
            json={"node1_id": 1, "node2_id": 2,
                  "iface1_id": 0, "iface2_id": 0, "linked": False},
        )
        assert resp.json() == {"result": True}

    def test_linked_true(self, client: TestClient, mock_bridge: MagicMock):
        resp = client.post(
            "/api/sessions/1/links/linked",
            json={"node1_id": 1, "node2_id": 2,
                  "iface1_id": 0, "iface2_id": 0, "linked": True},
        )
        assert resp.json() == {"result": True}


# =========================================================================
# WLAN / Mobility / Service / Wireless / EMANE / XML / Utility Routes
# =========================================================================

class TestWlanRoutes:
    def test_get(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_wlan_config.return_value = {"r": _mock_config_option()}
        resp = client.get("/api/sessions/1/wlan/1")
        assert resp.status_code == 200

    def test_set(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.set_wlan_config.return_value = True
        resp = client.post("/api/sessions/1/wlan",
                          json={"wlan_id": 1, "config": {"range": "300"}})
        assert resp.json() == {"result": True}


class TestMobilityRoutes:
    def test_get(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_mobility_config.return_value = {}
        resp = client.get("/api/sessions/1/mobility/1")
        assert resp.status_code == 200

    def test_set(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.set_mobility_config.return_value = True
        resp = client.post("/api/sessions/1/mobility",
                          json={"node_id": 1, "config": {"file": "/tmp/m.ns2"}})
        assert resp.json() == {"result": True}

    def test_action(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.mobility_action.return_value = True
        resp = client.post("/api/sessions/1/mobility/action",
                          json={"node_id": 1, "action": "START"})
        assert resp.json() == {"result": True}

    def test_action_invalid(self, client: TestClient, mock_bridge: MagicMock):
        resp = client.post("/api/sessions/1/mobility/action",
                          json={"node_id": 1, "action": "BAD"})
        assert resp.status_code == 400


class TestServiceRoutes:
    def test_defaults(self, client: TestClient, mock_bridge: MagicMock):
        d = MagicMock()
        d.templates = {"t.sh": "data"}
        d.config = {"k": _mock_config_option()}
        d.modes = []
        mock_bridge.get_service_defaults.return_value = d
        resp = client.get("/api/sessions/1/services/defaults?node_id=1&name=DR")
        assert resp.status_code == 200
        assert "templates" in resp.json()

    def test_node_svc(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_node_service.return_value = {}
        resp = client.get("/api/sessions/1/services/node?node_id=1&name=DR")
        assert resp.status_code == 200

    def test_rendered(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_service_rendered.return_value = {"f.sh": "content"}
        resp = client.get("/api/sessions/1/services/rendered?node_id=1&name=DR")
        assert resp.json() == {"rendered": {"f.sh": "content"}}

    def test_action(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.service_action.return_value = True
        resp = client.post("/api/sessions/1/services/action",
                          json={"node_id": 1, "service": "DR", "action": "VALIDATE"})
        assert resp.json() == {"result": True}

    def test_action_invalid(self, client: TestClient, mock_bridge: MagicMock):
        resp = client.post("/api/sessions/1/services/action",
                          json={"node_id": 1, "service": "DR", "action": "BAD"})
        assert resp.status_code == 400

    def test_create(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.create_service.return_value = True
        resp = client.post("/api/services/create",
                          json={"name": "svc", "group": "Test", "startup": ["echo ok"]})
        assert resp.json() == {"result": True}


class TestWirelessRoutes:
    def test_linked(self, client: TestClient, mock_bridge: MagicMock):
        resp = client.post("/api/sessions/1/wireless/linked",
                          json={"wireless_id": 5, "node1_id": 1, "node2_id": 2, "linked": True})
        assert resp.json() == {"result": True}

    def test_get_config(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_wireless_config.return_value = {}
        resp = client.get("/api/sessions/1/wireless/1")
        assert resp.status_code == 200


class TestEmaneRoutes:
    def test_get_model(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_emane_model_config.return_value = {}
        resp = client.get("/api/sessions/1/emane/model?node_id=1&model=rfpipe")
        assert resp.status_code == 200

    def test_channel(self, client: TestClient, mock_bridge: MagicMock):
        ch = MagicMock()
        ch.group = "224.0.0.1"; ch.port = 50000; ch.device = "eth0"
        mock_bridge.get_emane_event_channel.return_value = ch
        resp = client.get("/api/sessions/1/emane/channel?nem_id=0")
        assert resp.json()["group"] == "224.0.0.1"

    def test_link(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.emane_link.return_value = True
        resp = client.post("/api/sessions/1/emane/link",
                          json={"nem1": 1, "nem2": 2, "linked": True})
        assert resp.json() == {"result": True}


class TestXmlRoutes:
    def test_save(self, client: TestClient, mock_bridge: MagicMock):
        from pathlib import Path
        mock_bridge.save_xml.side_effect = lambda sid, path: Path(path).write_text("<scenario/>")
        resp = client.get("/api/sessions/1/xml")
        assert resp.status_code == 200

    def test_open(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.open_xml.return_value = (True, 5)
        resp = client.post("/api/xml/open",
                          json={"data": "<scenario/>", "start": False})
        assert resp.json()["result"] is True
        assert resp.json()["session_id"] == 5


class TestUtilityRoutes:
    def test_interfaces(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.get_interfaces.return_value = ["eth0", "wlan0"]
        resp = client.get("/api/interfaces")
        assert resp.json()["interfaces"] == ["eth0", "wlan0"]

    def test_execute(self, client: TestClient, mock_bridge: MagicMock):
        mock_bridge.execute_script.return_value = 3
        resp = client.post("/api/execute", json={"script": "print('hi')", "args": ""})
        assert resp.json()["session_id"] == 3


class TestGlobalRoutes:
    def test_config(self, client: TestClient, mock_bridge: MagicMock):
        cfg = MagicMock()
        cfg.services = []
        cfg.emane_models = []
        mock_bridge.get_config.return_value = cfg
        resp = client.get("/api/config")
        assert resp.status_code == 200
        assert resp.json()["services"] == []

    def test_config_with_services(self, client: TestClient, mock_bridge: MagicMock):
        svc = MagicMock()
        svc.name = "Zebra"; svc.group = "FRR"
        svc.executables = []; svc.directories = []; svc.files = []
        svc.startup = []; svc.validate = []; svc.shutdown = []
        cfg = MagicMock()
        cfg.services = [svc]
        cfg.emane_models = ["rfpipe"]
        mock_bridge.get_config.return_value = cfg
        resp = client.get("/api/config")
        assert resp.json()["services"][0]["name"] == "Zebra"
        assert resp.json()["emane_models"] == ["rfpipe"]
