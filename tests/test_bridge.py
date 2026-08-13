"""Unit tests for CoreGrpcBridge (mock gRPC, no daemon needed)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bridge import CoreGrpcBridge


# ---------------------------------------------------------------------------
# Init / Connection
# ---------------------------------------------------------------------------

class TestInit:
    def test_default_address(self):
        b = CoreGrpcBridge()
        assert b.address == "localhost:50051"
        assert b._client is None

    def test_custom_address(self):
        b = CoreGrpcBridge(address="10.0.0.1:50052")
        assert b.address == "10.0.0.1:50052"

    def test_client_property_raises_when_disconnected(self):
        b = CoreGrpcBridge()
        with pytest.raises(RuntimeError, match="not connected"):
            _ = b.client


class TestConnection:
    @patch("bridge.grpc_client.CoreGrpcClient", autospec=True)
    def test_connect(self, mock_cls):
        b = CoreGrpcBridge()
        mock = mock_cls.return_value
        b.connect()
        mock_cls.assert_called_once_with(address="localhost:50051")
        mock.connect.assert_called_once()
        assert b._client is mock

    @patch("bridge.grpc_client.CoreGrpcClient", autospec=True)
    def test_close(self, mock_cls):
        b = CoreGrpcBridge()
        b.connect()
        mock_client = b._client
        b.close()
        mock_client.close.assert_called_once()
        assert b._client is None

    @patch("bridge.grpc_client.CoreGrpcClient", autospec=True)
    def test_close_safe_when_not_connected(self, _mock_cls):
        CoreGrpcBridge().close()  # must not raise

    @patch("bridge.grpc_client.CoreGrpcClient", autospec=True)
    def test_context_manager(self, mock_cls):
        b = CoreGrpcBridge()
        mock = mock_cls.return_value
        with b.context() as ctx:
            assert ctx is b
            mock.connect.assert_called_once()
        mock.close.assert_called_once()
        assert b._client is None


# ---------------------------------------------------------------------------
# RPC forwarders
# ---------------------------------------------------------------------------

@pytest.fixture
def bridge():
    b = CoreGrpcBridge()
    b._client = MagicMock()
    yield b

class TestSessionRPCs:
    def test_create(self, bridge):
        bridge.create_session()
        bridge.client.create_session.assert_called_once_with(None)

    def test_create_with_id(self, bridge):
        bridge.create_session(42)
        bridge.client.create_session.assert_called_once_with(42)

    def test_delete(self, bridge):
        bridge.delete_session(1)
        bridge.client.delete_session.assert_called_once_with(1)

    def test_list(self, bridge):
        bridge.get_sessions()
        bridge.client.get_sessions.assert_called_once()

    def test_get(self, bridge):
        bridge.get_session(5)
        bridge.client.get_session.assert_called_once_with(5)

    def test_check(self, bridge):
        bridge.check_session(3)
        bridge.client.check_session.assert_called_once_with(3)

    def test_start(self, bridge):
        s = MagicMock()
        bridge.start_session(s, definition=True)
        bridge.client.start_session.assert_called_once_with(s, definition=True)

    def test_stop(self, bridge):
        bridge.stop_session(2)
        bridge.client.stop_session.assert_called_once_with(2)

    def test_alert(self, bridge):
        level = MagicMock()
        bridge.alert(1, level, "src", "msg", node_id=5)
        bridge.client.alert.assert_called_once_with(1, level, "src", "msg", 5)


class TestNodeRPCs:
    def test_add(self, bridge):
        n = MagicMock()
        bridge.add_node(1, n, source="gui")
        bridge.client.add_node.assert_called_once_with(1, n, "gui")

    def test_get(self, bridge):
        bridge.get_node(1, 2)
        bridge.client.get_node.assert_called_once_with(1, 2)

    def test_edit(self, bridge):
        bridge.edit_node(1, 2, icon="r.png")
        bridge.client.edit_node.assert_called_once_with(1, 2, "r.png", None)

    def test_delete(self, bridge):
        bridge.delete_node(1, 3)
        bridge.client.delete_node.assert_called_once_with(1, 3, None)

    def test_move_position(self, bridge):
        p = MagicMock()
        bridge.move_node(1, 2, position=p)
        bridge.client.move_node.assert_called_once_with(1, 2, p, None, None)

    def test_move_geo(self, bridge):
        g = MagicMock()
        bridge.move_node(1, 2, geo=g)
        bridge.client.move_node.assert_called_once_with(1, 2, None, g, None)

    def test_command(self, bridge):
        bridge.node_command(1, 2, "echo hi", wait=True, shell=False)
        bridge.client.node_command.assert_called_once_with(1, 2, "echo hi", True, False)

    def test_terminal(self, bridge):
        bridge.get_node_terminal(1, 2)
        bridge.client.get_node_terminal.assert_called_once_with(1, 2)


class TestLinkRPCs:
    def test_add(self, bridge):
        l = MagicMock()
        bridge.add_link(1, l, source="gui")
        bridge.client.add_link.assert_called_once_with(1, l, "gui")

    def test_edit(self, bridge):
        l = MagicMock()
        bridge.edit_link(1, l)
        bridge.client.edit_link.assert_called_once_with(1, l, None)

    def test_delete(self, bridge):
        l = MagicMock()
        bridge.delete_link(1, l)
        bridge.client.delete_link.assert_called_once_with(1, l, None)

    def test_linked(self, bridge):
        bridge.linked(1, 2, 3, 0, 1, True)
        bridge.client.linked.assert_called_once_with(1, 2, 3, 0, 1, True)


class TestWLANRPCs:
    def test_get_config(self, bridge):
        bridge.get_wlan_config(1, 2)
        bridge.client.get_wlan_config.assert_called_once_with(1, 2)

    def test_set_config(self, bridge):
        bridge.set_wlan_config(1, 2, {"range": "300"})
        bridge.client.set_wlan_config.assert_called_once_with(1, 2, {"range": "300"})

    def test_link(self, bridge):
        bridge.wlan_link(1, 4, 1, 2, True)
        bridge.client.wlan_link.assert_called_once_with(1, 4, 1, 2, True)


class TestMobilityRPCs:
    def test_get(self, bridge):
        bridge.get_mobility_config(1, 2)
        bridge.client.get_mobility_config.assert_called_once_with(1, 2)

    def test_set(self, bridge):
        bridge.set_mobility_config(1, 2, {"file": "/tmp/m.ns2"})
        bridge.client.set_mobility_config.assert_called_once_with(1, 2, {"file": "/tmp/m.ns2"})

    def test_action(self, bridge):
        a = MagicMock()
        bridge.mobility_action(1, 2, a)
        bridge.client.mobility_action.assert_called_once_with(1, 2, a)


class TestServiceRPCs:
    def test_defaults(self, bridge):
        bridge.get_service_defaults(1, 2, "DR")
        bridge.client.get_service_defaults.assert_called_once_with(1, 2, "DR")

    def test_node_svc(self, bridge):
        bridge.get_node_service(1, 2, "DR")
        bridge.client.get_node_service.assert_called_once_with(1, 2, "DR")

    def test_rendered(self, bridge):
        bridge.get_service_rendered(1, 2, "DR")
        bridge.client.get_service_rendered.assert_called_once_with(1, 2, "DR")

    def test_action(self, bridge):
        a = MagicMock()
        bridge.service_action(1, 2, "DR", a)
        bridge.client.service_action.assert_called_once_with(1, 2, "DR", a)

    def test_create(self, bridge):
        s = MagicMock()
        bridge.create_service(s, {"t": "d"}, recreate=True)
        bridge.client.create_service.assert_called_once_with(s, {"t": "d"}, True)


class TestWirelessRPCs:
    def test_linked(self, bridge):
        bridge.wireless_linked(1, 5, 1, 2, True)
        bridge.client.wireless_linked.assert_called_once_with(1, 5, 1, 2, True)

    def test_config(self, bridge):
        o = MagicMock()
        bridge.wireless_config(1, 5, 1, 2, o, o)
        bridge.client.wireless_config.assert_called_once_with(1, 5, 1, 2, o, o)

    def test_get_config(self, bridge):
        bridge.get_wireless_config(1, 5)
        bridge.client.get_wireless_config.assert_called_once_with(1, 5)


class TestEmaneRPCs:
    def test_get_model(self, bridge):
        bridge.get_emane_model_config(1, 2, "rfpipe")
        bridge.client.get_emane_model_config.assert_called_once_with(1, 2, "rfpipe", -1)

    def test_set_model(self, bridge):
        c = MagicMock()
        bridge.set_emane_model_config(1, c)
        bridge.client.set_emane_model_config.assert_called_once_with(1, c)

    def test_event_channel(self, bridge):
        bridge.get_emane_event_channel(1, 0)
        bridge.client.get_emane_event_channel.assert_called_once_with(1, 0)

    def test_link(self, bridge):
        bridge.emane_link(1, 1, 2, True)
        bridge.client.emane_link.assert_called_once_with(1, 1, 2, True)


class TestXmlRPCs:
    def test_save(self, bridge):
        bridge.save_xml(1, "/tmp/t.xml")
        bridge.client.save_xml.assert_called_once_with(1, "/tmp/t.xml")

    def test_open(self, bridge):
        p = MagicMock(spec=Path)
        bridge.open_xml(p, start=True)
        bridge.client.open_xml.assert_called_once_with(p, True)


class TestUtilityRPCs:
    def test_interfaces(self, bridge):
        bridge.get_interfaces()
        bridge.client.get_ifaces.assert_called_once()

    def test_execute(self, bridge):
        bridge.execute_script("/tmp/s.py", "arg")
        bridge.client.execute_script.assert_called_once_with("/tmp/s.py", "arg")


class TestGlobalRPCs:
    def test_config(self, bridge):
        bridge.get_config()
        bridge.client.get_config.assert_called_once()


class TestStreamingFactories:
    def test_events(self, bridge):
        assert bridge.events(1) is not None

    def test_throughputs(self, bridge):
        assert bridge.throughputs(1) is not None

    def test_cpu_usage(self, bridge):
        assert bridge.cpu_usage(1) is not None

    def test_move_nodes_streamer(self, bridge):
        assert bridge.move_nodes_streamer(1) is not None

    def test_emane_pathlosses_streamer(self, bridge):
        assert bridge.emane_pathlosses_streamer() is not None

    def test_emane_events_streamer(self, bridge):
        assert bridge.emane_events_streamer() is not None