# Changelog

## v1.1.4 (2026-08-20)

- Frontend: right-click context menu on nodes (Properties / Terminal / Copy / Delete)
- Frontend: node property editor modal (name, model, type, position, services)
- Frontend: WebSocket interactive terminal (PTY bash via vcmd -i)
- Frontend: localStorage persistence for custom properties
- Frontend: log/events panel preservation across tab switches
- Fix: missing websockets library (added to wheels)
- Fix: vcmd -q flag removed (was suppressing terminal I/O)
- Bundle: includes static/ frontend in offline deployment

## v1.1.3 (2026-08-19)

- Frontend SPA with topology canvas, drag-and-drop nodes
- API endpoint for WebSocket interactive terminal
- Root route ("/") serves the frontend
- Non-blocking NodeCommand via asyncio.to_thread

## v1.1.2 (2026-08-18)

- Comprehensive README with per-endpoint parameter tables

## v1.1.1 (2026-08-17)

- Topology link supports custom interface IPs (iface1_ip4/iface2_ip4)
- XML topology parsing supports iface1_ip4 attributes

## v1.1.0 (2026-08-17)

- Fix GetSession returning 500 after adding nodes
- Stop preserves topology definition
- uvicorn[standard] wheels (20 wheels)
- Complete README with API docs + topology tutorial

## v1.0.0 (2026-08-14)

- Initial release
- 40+ HTTP endpoints covering all CoreApi RPCs
- JSON/XML topology import
- SSE/WebSocket streaming
- 143 unit tests
- Offline deployment bundle