# Changelog

## v1.0.0 (2026-08-14)

- Initial release
- gRPC ↔ HTTP bridge for all 42 CORE CoreApi RPCs
- RESTful endpoints for Session, Node, Link, WLAN, Mobility, Service, Wireless, EMANE, XML, Utility, Global APIs
- JSON and XML topology import (`POST /api/topology`)
- SSE streaming for Events, Throughputs, CpuUsage
- WebSocket streaming for MoveNodes, EmanePathlosses, EmaneEvents
- Unified error responses with Chinese hints
- 143 unit tests