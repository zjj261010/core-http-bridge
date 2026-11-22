"""Loopback-only mock for IF-02 and IF-03 integration."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sqlite3


ENDPOINTS = {
    '/api/v1/execution-results': ('results', 'received_steps'),
    '/api/v1/snort-alerts': ('alerts', 'received_alerts'),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18082)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--fail-first', action='store_true', help='确认丢失模拟，首次入库后返回 503')
    args = parser.parse_args()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.db) as database:
        database.execute("""CREATE TABLE IF NOT EXISTS receipts(
            scenario_id TEXT, sequence_id TEXT, batch_no INTEGER, payload TEXT, attempts INTEGER,
            PRIMARY KEY(scenario_id,sequence_id,batch_no))""")

    class Receiver(BaseHTTPRequestHandler):
        def respond(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            endpoint = ENDPOINTS.get(self.path)
            if endpoint is None:
                return self.respond(404, {'code': 404})
            collection, count_field = endpoint
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 * 1024:
                    raise ValueError('invalid size')
                raw = self.rfile.read(size)
                payload = json.loads(raw)
                scenario, sequence = payload['scenario_id'], payload['sequence_id']
                batch = payload['batch_no'] if collection == 'alerts' else 0
                if (not isinstance(scenario, str) or not isinstance(sequence, str)
                        or type(batch) is not int or (collection == 'alerts' and batch < 1)
                        or not isinstance(payload[collection], list)):
                    raise ValueError('invalid payload')
            except (ValueError, TypeError, KeyError, UnicodeError):
                return self.respond(400, {'code': 1004})
            key = scenario, sequence, batch
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            with sqlite3.connect(args.db) as database:
                row = database.execute('SELECT payload FROM receipts WHERE scenario_id=? AND sequence_id=? AND batch_no=?',
                                       key).fetchone()
                if row and row[0] != canonical:
                    return self.respond(409, {'code': 1005})
                if batch and not database.execute(
                        'SELECT 1 FROM receipts WHERE scenario_id=? AND sequence_id=? AND batch_no=?',
                        (scenario, sequence, batch - 1)).fetchone():
                    return self.respond(409, {'code': 1006})
                database.execute("""INSERT INTO receipts VALUES(?,?,?,?,1)
                    ON CONFLICT(scenario_id,sequence_id,batch_no) DO UPDATE SET attempts=attempts+1""", (*key, canonical))
            if not row and args.fail_first:
                return self.respond(503, {'code': 1501})
            return self.respond(200, {'code': 0, count_field: len(payload[collection])})

    with HTTPServer(('127.0.0.1', args.port), Receiver) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
