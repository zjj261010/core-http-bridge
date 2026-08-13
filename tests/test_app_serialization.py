"""Unit tests for serialization helpers and Pydantic models in app.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app import (
    PositionModel, GeoModel, InterfaceModel, LinkOptionsModel, LinkModel, NodeModel,
    _session_to_dict, _session_summary_to_dict, _node_to_dict, _link_to_dict,
    _iface_to_dict, _link_options_to_dict, _config_option_to_dict,
    _config_option_dict_to_dict, _wrappers_node, _wrappers_iface, _wrappers_link_opts,
    _error_response, _parse_grpc_error, _hint_from_detail,
)


def _make_grpc_error(grpc_status: int, message: str):
    _ErrClass = type("_InactiveRpcError", (Exception,), {})
    debug = (
        'UNKNOWN:Error received from peer  {'
        f'grpc_message:"{message}", grpc_status:{grpc_status},'
        'created_time:"2026-01-01T00:00:00+00:00"'
        '}'
    )
    return _ErrClass(debug)


class TestModels:
    def test_position_defaults(self):
        p = PositionModel()
        assert p.x == 0 and p.y == 0

    def test_position_custom(self):
        p = PositionModel(x=10.5, y=20.3)
        assert p.x == 10.5 and p.y == 20.3

    def test_position_validation(self):
        with pytest.raises(ValidationError):
            PositionModel(x="abc")

    def test_geo_defaults(self):
        g = GeoModel()
        assert g.lat == 0 and g.lon == 0 and g.alt == 0

    def test_interface_custom(self):
        i = InterfaceModel(id=1, name="eth0", ip4="10.0.0.1", ip4_mask=24)
        assert i.ip4 == "10.0.0.1"

    def test_link_options_custom(self):
        o = LinkOptionsModel(bandwidth=10000000, delay=500, loss=1.5)
        assert o.bandwidth == 10000000 and o.loss == 1.5

    def test_link_required(self):
        with pytest.raises(ValidationError):
            LinkModel()

    def test_link_valid(self):
        l = LinkModel(node1_id=1, node2_id=2)
        assert l.node1_id == 1

    def test_link_with_ifaces(self):
        LinkModel(node1_id=1, node2_id=2,
                  iface1=InterfaceModel(id=0), iface2=InterfaceModel(id=0),
                  options=LinkOptionsModel(bandwidth=54000000))

    def test_node_default_type(self):
        n = NodeModel(id=1, name="n1")
        assert n.type == 0

    def test_node_with_switch(self):
        n = NodeModel(id=1, name="sw1", type=4)
        assert n.type == 4


class TestSerialization:
    def test_session_to_dict(self):
        s = MagicMock()
        s.id = 1; type(s.state).value = 3
        s.nodes = []; s.links = []; s.dir = "/tmp/pycore.1"
        s.user = "root"; s.file = None
        d = _session_to_dict(s)
        assert d["id"] == 1 and d["state"] == 3

    def test_session_summary(self):
        s = MagicMock()
        s.id = 1; type(s.state).value = 1
        s.nodes = 5; s.file = "/tmp/s.xml"; s.dir = "/tmp"
        d = _session_summary_to_dict(s)
        assert d["nodes"] == 5

    def test_node_to_dict(self):
        n = MagicMock()
        n.id = 1; n.name = "n1"; type(n.type).value = 0
        n.model = "PC"
        n.position = MagicMock(x=100, y=200)
        n.geo = None; n.services = {"DefaultRoute"}
        n.emane = n.icon = n.image = n.server = n.dir = n.channel = ""
        n.canvas = 0
        d = _node_to_dict(n)
        assert d["name"] == "n1" and d["position"]["x"] == 100

    def test_iface_to_dict(self):
        i = MagicMock()
        i.id = 0; i.name = "eth0"; i.ip4 = "10.0.0.1"; i.ip4_mask = 24
        i.ip6 = ""; i.ip6_mask = 0; i.mtu = 1500; i.node_id = 0; i.mac = ""
        d = _iface_to_dict(i)
        assert d["ip4"] == "10.0.0.1"


class TestWrappersConversion:
    def test_node_default(self):
        w = _wrappers_node(NodeModel(id=1, name="n1"))
        assert w.id == 1 and w.name == "n1"

    def test_node_with_position(self):
        w = _wrappers_node(NodeModel(id=1, name="n1", position=PositionModel(x=50, y=60)))
        assert w.position.x == 50

    def test_iface_none(self):
        assert _wrappers_iface(None) is None

    def test_iface_valid(self):
        w = _wrappers_iface(InterfaceModel(id=0, name="eth0", ip4="10.0.0.1"))
        assert w.id == 0 and w.ip4 == "10.0.0.1"

    def test_link_opts_none(self):
        assert _wrappers_link_opts(None) is None

    def test_link_opts_valid(self):
        o = _wrappers_link_opts(LinkOptionsModel(bandwidth=10000000, delay=500))
        assert o.bandwidth == 10000000


class TestErrorResponse:
    def test_basic(self):
        r = _error_response(404, "not found")
        assert r.status_code == 404
        assert json.loads(r.body)["error"] == "not found"

    def test_with_detail(self):
        r = _error_response(400, "bad input", detail="invalid", hint="check docs")
        body = json.loads(r.body)
        assert body["detail"] == "invalid"
        assert body["hint"] == "check docs"


class TestGrpcErrorParsing:
    def test_non_grpc(self):
        assert _parse_grpc_error(ValueError("x")) is None

    def test_not_found(self):
        r = _parse_grpc_error(_make_grpc_error(5, "session 99 not found"))
        assert r is not None
        assert r["http"] == 404
        assert r["status"] == "NOT_FOUND"

    def test_invalid_argument(self):
        r = _parse_grpc_error(_make_grpc_error(3, "bad request"))
        assert r["http"] == 400
        assert r["status"] == "INVALID_ARGUMENT"

    def test_unavailable(self):
        r = _parse_grpc_error(_make_grpc_error(14, "unavailable"))
        assert r["http"] == 503

    def test_unimplemented(self):
        r = _parse_grpc_error(_make_grpc_error(12, "not implemented"))
        assert r["http"] == 501


class TestHintFromDetail:
    def test_session_not_found(self):
        assert "session_id 不存在" in _hint_from_detail("session 123 not found")

    def test_node_not_found(self):
        assert "node_id 不存在" in _hint_from_detail("node 5 not found")

    def test_interface_missing(self):
        assert "iface_id 不存在" in _hint_from_detail("does not have interface 99")

    def test_wlan(self):
        assert "WLAN" in _hint_from_detail("wlan node 4 is not using BasicRangeModel")

    def test_mobility(self):
        assert "移动" in _hint_from_detail("node(wlan1) does not have mobility")

    def test_ptp(self):
        assert "重复" in _hint_from_detail("ptp links support at most 2 interfaces")

    def test_none_type(self):
        assert "格式" in _hint_from_detail("'NoneType' object has no attribute 'to_proto'")

    def test_unknown(self):
        assert "检查" in _hint_from_detail("something about unknown resource")

    def test_no_match(self):
        assert _hint_from_detail("completely unrelated error") == ""
