"""Simple paced HTTP GET client. Executed only inside a CORE node namespace."""
import concurrent.futures
import http.client
import json
import socket
import sys
import time


def report(status, **metrics):
    print(json.dumps({'status': status, **metrics}), flush=True)


def request(address, port, path):
    connection = http.client.HTTPConnection(address, port, timeout=1)
    try:
        connection.request('GET', path, headers={'Connection': 'close', 'User-Agent': 'CORE Background Traffic'})
        response = connection.getresponse()
        data = response.read(64 * 1024)
        return int(response.status >= 400), len(data)
    except (OSError, http.client.HTTPException):
        return 1, 0
    finally:
        connection.close()


def run(address, port, path, rate, duration):
    report('starting')
    deadline = time.monotonic() + 15
    while True:
        try:
            with socket.create_connection((address, port), timeout=0.5):
                break
        except OSError:
            if time.monotonic() >= deadline:
                raise SystemExit('目标 HTTP 服务未在 15 秒内监听')
            time.sleep(0.25)
    total = errors = size = skipped = 0
    start = last_sample = time.monotonic()
    end = start + duration
    next_request = start
    last_total = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        pending = set()
        while time.monotonic() < end or pending:
            now = time.monotonic()
            for future in list(pending):
                if future.done():
                    failed, received = future.result()
                    total += 1
                    errors += failed
                    size += received
                    pending.remove(future)
            if now < end and now >= next_request:
                if len(pending) < 8:
                    pending.add(pool.submit(request, address, port, path))
                else:
                    skipped += 1
                next_request = now + 1 / rate
            if now - last_sample >= 1 or now >= end and not pending:
                report('running', requests=total, errors=errors, bytes=size, skipped=skipped,
                       requests_per_second=round((total - last_total) / max(0.01, now - last_sample), 2))
                last_sample, last_total = now, total
            if now < end or pending:
                time.sleep(max(0.001, min(0.02, next_request - now)) if now < end else 0.02)


if __name__ == '__main__':
    host, port, path, qps, duration = sys.argv[1:]
    run(host, int(port), path, int(qps), int(duration))
