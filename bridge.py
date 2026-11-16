"""
CoreGrpcBridge — 对 CORE gRPC 接口的同步封装层。

管理 CoreGrpcClient 连接生命周期，将 gRPC stub 调用转为
Python 原生对象，供 FastAPI route 调用。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Generator, Iterator

from core.api.grpc import client as grpc_client
from core.api.grpc import wrappers

logger = logging.getLogger(__name__)


class CoreGrpcBridge:
    """对 CoreGrpcClient 的高层封装，提供便捷的连接管理。"""

    def __init__(self, address: str = "localhost:50051") -> None:
        self.address = address
        self._client: grpc_client.CoreGrpcClient | None = None

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    @property
    def client(self) -> grpc_client.CoreGrpcClient:
        if self._client is None:
            raise RuntimeError("bridge not connected — call .connect() first")
        return self._client

    def connect(self) -> None:
        self._client = grpc_client.CoreGrpcClient(address=self.address)
        self._client.connect()
        logger.info("connected to core-daemon at %s", self.address)

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
            logger.info("disconnected from core-daemon")

    @contextmanager
    def context(self) -> Generator[CoreGrpcBridge, None, None]:
        try:
            self.connect()
            yield self
        finally:
            self.close()

    # ------------------------------------------------------------------
    # Session RPCs
    # ------------------------------------------------------------------

    def create_session(self, session_id: int | None = None) -> wrappers.Session:
        return self.client.create_session(session_id)

    def delete_session(self, session_id: int) -> bool:
        return self.client.delete_session(session_id)

    def get_sessions(self) -> list[wrappers.SessionSummary]:
        return self.client.get_sessions()

    def get_session(self, session_id: int) -> wrappers.Session:
        return self.client.get_session(session_id)

    def check_session(self, session_id: int) -> bool:
        return self.client.check_session(session_id)

    def start_session(
        self, session: wrappers.Session, definition: bool = False
    ) -> tuple[bool, list[str]]:
        return self.client.start_session(session, definition=definition)

    def stop_session(self, session_id: int) -> bool:
        return self.client.stop_session(session_id)

    def alert(
        self,
        session_id: int,
        level: wrappers.AlertLevel,
        source: str,
        text: str,
        node_id: int | None = None,
    ) -> bool:
        return self.client.alert(session_id, level, source, text, node_id)

    # ------------------------------------------------------------------
    # Node RPCs
    # ------------------------------------------------------------------

    def add_node(
        self, session_id: int, node: wrappers.Node, source: str | None = None
    ) -> int:
        return self.client.add_node(session_id, node, source)

    def get_node(
        self, session_id: int, node_id: int
    ) -> tuple[wrappers.Node, list[wrappers.Interface], list[wrappers.Link]]:
        return self.client.get_node(session_id, node_id)

    def edit_node(
        self,
        session_id: int,
        node_id: int,
        icon: str | None = None,
        source: str | None = None,
    ) -> bool:
        return self.client.edit_node(session_id, node_id, icon, source)

    def delete_node(
        self, session_id: int, node_id: int, source: str | None = None
    ) -> bool:
        return self.client.delete_node(session_id, node_id, source)

    def move_node(
        self,
        session_id: int,
        node_id: int,
        position: wrappers.Position | None = None,
        geo: wrappers.Geo | None = None,
        source: str | None = None,
    ) -> bool:
        return self.client.move_node(session_id, node_id, position, geo, source)

    def node_command(
        self,
        session_id: int,
        node_id: int,
        command: str,
        wait: bool = True,
        shell: bool = False,
    ) -> tuple[int, str]:
        return self.client.node_command(session_id, node_id, command, wait, shell)

    def get_node_terminal(self, session_id: int, node_id: int) -> str:
        return self.client.get_node_terminal(session_id, node_id)

    # ------------------------------------------------------------------
    # Link RPCs
    # ------------------------------------------------------------------

    def add_link(
        self, session_id: int, link: wrappers.Link, source: str | None = None
    ) -> tuple[bool, wrappers.Interface | None, wrappers.Interface | None]:
        return self.client.add_link(session_id, link, source)

    def edit_link(
        self, session_id: int, link: wrappers.Link, source: str | None = None
    ) -> bool:
        return self.client.edit_link(session_id, link, source)

    def delete_link(
        self, session_id: int, link: wrappers.Link, source: str | None = None
    ) -> bool:
        return self.client.delete_link(session_id, link, source)

    def linked(
        self,
        session_id: int,
        node1_id: int,
        node2_id: int,
        iface1_id: int,
        iface2_id: int,
        linked: bool,
    ) -> None:
        self.client.linked(session_id, node1_id, node2_id, iface1_id, iface2_id, linked)

    # ------------------------------------------------------------------
    # WLAN RPCs
    # ------------------------------------------------------------------

    def get_wlan_config(
        self, session_id: int, node_id: int
    ) -> dict[str, wrappers.ConfigOption]:
        return self.client.get_wlan_config(session_id, node_id)

    def set_wlan_config(
        self, session_id: int, node_id: int, config: dict[str, str]
    ) -> bool:
        return self.client.set_wlan_config(session_id, node_id, config)

    def wlan_link(
        self,
        session_id: int,
        wlan_id: int,
        node1_id: int,
        node2_id: int,
        linked: bool,
    ) -> bool:
        return self.client.wlan_link(session_id, wlan_id, node1_id, node2_id, linked)

    # ------------------------------------------------------------------
    # Mobility RPCs
    # ------------------------------------------------------------------

    def get_mobility_config(
        self, session_id: int, node_id: int
    ) -> dict[str, wrappers.ConfigOption]:
        return self.client.get_mobility_config(session_id, node_id)

    def set_mobility_config(
        self, session_id: int, node_id: int, config: dict[str, str]
    ) -> bool:
        return self.client.set_mobility_config(session_id, node_id, config)

    def mobility_action(
        self,
        session_id: int,
        node_id: int,
        action: wrappers.MobilityAction,
    ) -> bool:
        return self.client.mobility_action(session_id, node_id, action)

    # ------------------------------------------------------------------
    # Service RPCs
    # ------------------------------------------------------------------

    def get_service_defaults(
        self, session_id: int, node_id: int, name: str
    ) -> wrappers.ServiceDefaults:
        return self.client.get_service_defaults(session_id, node_id, name)

    def get_node_service(
        self, session_id: int, node_id: int, name: str
    ) -> dict[str, str]:
        return self.client.get_node_service(session_id, node_id, name)

    def get_service_rendered(
        self, session_id: int, node_id: int, name: str
    ) -> dict[str, str]:
        return self.client.get_service_rendered(session_id, node_id, name)

    def service_action(
        self,
        session_id: int,
        node_id: int,
        service: str,
        action: wrappers.ServiceAction,
    ) -> bool:
        return self.client.service_action(session_id, node_id, service, action)

    def create_service(
        self,
        service: wrappers.Service,
        templates: dict[str, str],
        recreate: bool = False,
    ) -> bool:
        return self.client.create_service(service, templates, recreate)

    # ------------------------------------------------------------------
    # Wireless RPCs
    # ------------------------------------------------------------------

    def wireless_linked(
        self,
        session_id: int,
        wireless_id: int,
        node1_id: int,
        node2_id: int,
        linked: bool,
    ) -> None:
        self.client.wireless_linked(session_id, wireless_id, node1_id, node2_id, linked)

    def wireless_config(
        self,
        session_id: int,
        wireless_id: int,
        node1_id: int,
        node2_id: int,
        options1: wrappers.LinkOptions,
        options2: wrappers.LinkOptions | None = None,
    ) -> None:
        self.client.wireless_config(
            session_id, wireless_id, node1_id, node2_id, options1, options2
        )

    def get_wireless_config(
        self, session_id: int, node_id: int
    ) -> dict[str, wrappers.ConfigOption]:
        return self.client.get_wireless_config(session_id, node_id)

    # ------------------------------------------------------------------
    # EMANE RPCs
    # ------------------------------------------------------------------

    def get_emane_model_config(
        self, session_id: int, node_id: int, model: str, iface_id: int = -1
    ) -> dict[str, wrappers.ConfigOption]:
        return self.client.get_emane_model_config(session_id, node_id, model, iface_id)

    def set_emane_model_config(
        self,
        session_id: int,
        emane_model_config: wrappers.EmaneModelConfig,
    ) -> bool:
        return self.client.set_emane_model_config(session_id, emane_model_config)

    def get_emane_event_channel(
        self, session_id: int, nem_id: int
    ) -> wrappers.EmaneEventChannel:
        return self.client.get_emane_event_channel(session_id, nem_id)

    def emane_link(
        self, session_id: int, nem1: int, nem2: int, linked: bool
    ) -> bool:
        return self.client.emane_link(session_id, nem1, nem2, linked)

    # ------------------------------------------------------------------
    # XML RPCs
    # ------------------------------------------------------------------

    def save_xml(self, session_id: int, file_path: str) -> None:
        self.client.save_xml(session_id, file_path)

    def open_xml(self, file_path: Path, start: bool = False) -> tuple[bool, int]:
        return self.client.open_xml(file_path, start)

    # ------------------------------------------------------------------
    # Utility RPCs
    # ------------------------------------------------------------------

    def get_interfaces(self) -> list[str]:
        return self.client.get_ifaces()

    def execute_script(self, script: str, args: str = "") -> int | None:
        return self.client.execute_script(script, args)

    # ------------------------------------------------------------------
    # Global RPCs
    # ------------------------------------------------------------------

    def get_config(self) -> wrappers.CoreConfig:
        return self.client.get_config()

    # ------------------------------------------------------------------
    # Streaming — server-stream (返回 iterator 供 SSE 消费)
    # ------------------------------------------------------------------

    def events(
        self,
        session_id: int,
        event_types: list[wrappers.EventType] | None = None,
        handler: Callable[[wrappers.Event], None] | None = None,
    ) -> grpc_client.CoreGrpcClient._CoreGrpcClient__EventsCall:
        """返回 grpc 流对象（CORE 客户端起后台线程驱动 handler）。"""
        if event_types is None:
            event_types = []
        return self.client.events(
            session_id,
            handler or (lambda e: None),
            event_types,
        )

    def throughputs(
        self, session_id: int, handler: Callable[[wrappers.ThroughputsEvent], None] | None = None
    ) -> grpc_client.CoreGrpcClient._CoreGrpcClient__ThroughputsCall:
        return self.client.throughputs(session_id, handler or (lambda e: None))

    def cpu_usage(
        self, delay: int, handler: Callable[[wrappers.CpuUsageEvent], None] | None = None
    ) -> grpc_client.CoreGrpcClient._CoreGrpcClient__CpuUsageCall:
        return self.client.cpu_usage(delay, handler or (lambda e: None))

    # ------------------------------------------------------------------
    # Streaming — client-stream (返回 streamer 对象供 WebSocket 驱动)
    # ------------------------------------------------------------------

    def move_nodes_streamer(self, session_id: int) -> grpc_client.MoveNodesStreamer:
        return grpc_client.MoveNodesStreamer(session_id)

    def move_nodes(self, streamer: grpc_client.MoveNodesStreamer) -> None:
        self.client.move_nodes(streamer)

    def emane_pathlosses_streamer(self) -> grpc_client.EmanePathlossesStreamer:
        return grpc_client.EmanePathlossesStreamer()

    def emane_pathlosses(self, streamer: grpc_client.EmanePathlossesStreamer) -> None:
        self.client.emane_pathlosses(streamer)

    def emane_events_streamer(self) -> grpc_client.EmaneEventsStreamer:
        return grpc_client.EmaneEventsStreamer()

    def emane_events(self, streamer: grpc_client.EmaneEventsStreamer) -> None:
        self.client.emane_events(streamer)