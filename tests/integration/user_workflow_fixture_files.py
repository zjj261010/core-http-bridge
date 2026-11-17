"""Write local script inputs for the UI-only workflow repair acceptance; no HTTP or Docker operations."""
import argparse
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--run-dir',required=True)
args=parser.parse_args()
root=Path(args.run_dir).resolve()
if (root/'ARCHIVED').exists():
    raise SystemExit('历史目录只读，请准备新的验收目录')
fixtures=root/'fixtures'
fixtures.mkdir(parents=True,exist_ok=True)
(fixtures/'serve.sh').write_text('#!/bin/sh\npython3 -m http.server 8080 --bind 0.0.0.0 >/tmp/repair-http.log 2>&1 &\n')
(fixtures/'ready.sh').write_text('#!/bin/sh\npython3 -c "import socket; socket.create_connection((\'127.0.0.1\',8080),1).close()"\n')
print('仅生成serve.sh和ready.sh，服务配置与启动由页面验收完成。')
