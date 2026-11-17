"""Consistent SQLite backup plus legacy metadata and service definitions."""
import argparse,json,shutil,sqlite3,time
from pathlib import Path

parser=argparse.ArgumentParser();parser.add_argument('--data-dir',default='/opt/artifacts/core-drill');parser.add_argument('--services-dir',default='/opt/core/etc/custom_services');parser.add_argument('--output',required=True);parser.add_argument('--db-path')
a=parser.parse_args();data=Path(a.data_dir);out=Path(a.output);out.mkdir(parents=True,exist_ok=False)
database=Path(a.db_path) if a.db_path else data/'platform.db'
if database.exists():
    source=sqlite3.connect(str(database));backup=sqlite3.connect(str(out/'platform.db'))
    source.backup(backup)
    result=backup.execute('PRAGMA integrity_check').fetchone()[0]
    backup.close();source.close()
    if result!='ok': raise RuntimeError('SQLite backup integrity check: '+result)
for name in ('manifests.json','instances.json','topologies.json','audit.jsonl'):
    if (data/name).exists(): shutil.copy2(data/name,out/name)
if Path(a.services_dir).is_dir(): shutil.copytree(a.services_dir,out/'custom_services',ignore=shutil.ignore_patterns('__pycache__'))
(out/'backup.json').write_text(json.dumps({'created_at':time.strftime('%Y-%m-%d %H:%M:%S'),'data_dir':str(data),'services_dir':a.services_dir,'sqlite_backup_api':True},indent=2))
print(out)
