"""Consistent SQLite backup plus legacy metadata and service definitions."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import time

parser = argparse.ArgumentParser()
parser.add_argument('--data-dir', default='/opt/artifacts/core-drill')
parser.add_argument('--services-dir', default='/opt/core/etc/custom_services')
parser.add_argument('--output', required=True)
parser.add_argument('--db-path')
args = parser.parse_args()
data = Path(args.data_dir)
output = Path(args.output)
database = (Path(args.db_path) if args.db_path else data / 'platform.db').resolve()
if not database.is_file() or not database.stat().st_size:
    parser.error(f'平台数据库不存在或为空：{database}')

with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
    output.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect(output / 'platform.db')) as backup:
        source.backup(backup)
        result = backup.execute('PRAGMA integrity_check').fetchone()[0]
        if result != 'ok':
            raise RuntimeError('SQLite backup integrity check: ' + result)

for name in ('manifests.json', 'instances.json', 'topologies.json', 'audit.jsonl'):
    if (data / name).exists():
        shutil.copy2(data / name, output / name)
if Path(args.services_dir).is_dir():
    shutil.copytree(args.services_dir, output / 'custom_services', ignore=shutil.ignore_patterns('__pycache__'))
(output / 'backup.json').write_text(json.dumps({
    'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    'data_dir': str(data), 'db_path': str(database), 'services_dir': args.services_dir,
    'sqlite_backup_api': True,
}, indent=2))
print(output)
