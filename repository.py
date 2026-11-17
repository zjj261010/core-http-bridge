"""SQLite documents and immutable published revisions. JSON is migration input only."""
import contextlib
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path
import platform_db
import storage
import database
import threading

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS documents(collection TEXT, name TEXT, body TEXT NOT NULL, PRIMARY KEY(collection,name));
CREATE TABLE IF NOT EXISTS migrations(source TEXT PRIMARY KEY, backup TEXT, count INTEGER, migrated_at TEXT);
CREATE TABLE IF NOT EXISTS migration_reports(collection TEXT PRIMARY KEY, report TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions(kind TEXT, name TEXT, version INTEGER, body TEXT NOT NULL, created_at TEXT, PRIMARY KEY(kind,name,version));
CREATE TABLE IF NOT EXISTS revision_dependencies(
 owner_kind TEXT, owner_name TEXT, owner_version INTEGER, slot TEXT,
 target_kind TEXT NOT NULL,target_name TEXT NOT NULL,target_version INTEGER NOT NULL,
 PRIMARY KEY(owner_kind,owner_name,owner_version,slot),
 FOREIGN KEY(owner_kind,owner_name,owner_version) REFERENCES revisions(kind,name,version),
 FOREIGN KEY(target_kind,target_name,target_version) REFERENCES revisions(kind,name,version));
CREATE TABLE IF NOT EXISTS document_revision_refs(
 collection TEXT,name TEXT,slot TEXT,kind TEXT NOT NULL,target_name TEXT NOT NULL,version INTEGER NOT NULL,
 PRIMARY KEY(collection,name,slot),
 FOREIGN KEY(collection,name) REFERENCES documents(collection,name) ON DELETE CASCADE,
 FOREIGN KEY(kind,target_name,version) REFERENCES revisions(kind,name,version));
CREATE TABLE IF NOT EXISTS reference_issues(collection TEXT,name TEXT,slot TEXT,detail TEXT,PRIMARY KEY(collection,name,slot));
CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY,applied_at TEXT);
'''

_schema_lock=threading.RLock()


def initialize():
    platform_db.init()
    if database.current(platform_db.DB_PATH): return
    with _schema_lock, database.connection(platform_db.DB_PATH) as c:
        table=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
        if table and c.execute("SELECT 1 FROM schema_migrations WHERE name='plan-closure-v1'").fetchone(): return
        c.executescript(_SCHEMA)
        columns={r[1] for r in c.execute('PRAGMA table_info(revisions)')}
        if 'publication_state' not in columns:
            c.execute("ALTER TABLE revisions ADD COLUMN publication_state TEXT NOT NULL DEFAULT 'published' CHECK(publication_state='published')")
        c.execute("CREATE TRIGGER IF NOT EXISTS published_immutable_update BEFORE UPDATE ON revisions BEGIN SELECT RAISE(ABORT,'published revision is immutable'); END")
        c.execute("CREATE TRIGGER IF NOT EXISTS published_immutable_delete BEFORE DELETE ON revisions BEGIN SELECT RAISE(ABORT,'published revision is immutable'); END")
        duplicates=c.execute("SELECT name,body FROM documents WHERE collection='deployments' AND json_extract(body,'$.status') IN ('defined','running','stopped') AND rowid NOT IN (SELECT MAX(rowid) FROM documents WHERE collection='deployments' AND json_extract(body,'$.status') IN ('defined','running','stopped') GROUP BY json_extract(body,'$.drill_id'))").fetchall()
        for row in duplicates:
            original=json.loads(row['body']);original['status']='interrupted'
            c.execute('UPDATE documents SET body=? WHERE collection=? AND name=?',(json.dumps(original),'deployments',row['name']))
            c.execute('INSERT OR REPLACE INTO reference_issues VALUES(?,?,?,?)',('deployments',row['name'],'legacy-duplicate','同演练重复活动部署，原快照保留并标记中断'))
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_deployment_per_drill ON documents(json_extract(body,'$.drill_id')) WHERE collection='deployments' AND json_extract(body,'$.status') IN ('defined','running','stopped')")
        if not c.execute("SELECT 1 FROM schema_migrations WHERE name='revision-references-v1'").fetchone():
            for r in c.execute('SELECT collection,name,body FROM documents').fetchall():
                _sync_refs(c,r['collection'],r['name'],json.loads(r['body']),legacy=True)
            for r in c.execute('SELECT kind,name,version,body FROM revisions').fetchall():
                _revision_refs(c,r['kind'],r['name'],r['version'],json.loads(r['body']),legacy=True)
            c.execute("INSERT INTO schema_migrations VALUES('revision-references-v1',?)",(now(),))
        c.execute("INSERT OR IGNORE INTO schema_migrations VALUES('plan-closure-v1',?)",(now(),))


@contextlib.contextmanager
def connection():
    initialize()
    with database.connection(platform_db.DB_PATH) as c: yield c


@contextlib.contextmanager
def transaction():
    initialize()
    with database.transaction(platform_db.DB_PATH) as c: yield c


def _references(collection,body):
    if not isinstance(body,dict): return []
    refs=[]
    if collection=='instances' and body.get('version'):
        # name is supplied by _sync_refs.
        refs.append(('self','template',None,body['version']))
    if collection=='manifests' and body.get('target_revision'):
        refs.append(('self','target',None,body['target_revision']))
    if collection=='publication_drafts':
        payload=body.get('body') or {}
        if body.get('kind')=='template' and payload.get('target_name') and payload.get('target_version'):
            refs.append(('target','target',payload['target_name'],payload['target_version']))
        if body.get('status')=='published': refs.append(('published',body['kind'],body['name'],body['published_version']))
    definitions=[]
    if collection in ('topologies','topology_revisions'):
        definitions=[('definition',body.get('definition') or {})]
    elif collection=='session_workspaces':
        definitions=[('bindings',{'nodes':[dict(n,id=k) for k,n in body.get('bindings',{}).items()]})]
    elif collection=='deployments':
        definitions=[('source',body.get('source') or {}),('snapshot',body.get('snapshot') or {})]
        definitions.append(('bindings',{'nodes':[dict(n,id=k) for k,n in body.get('bindings',{}).items()]}))
    for prefix,definition in definitions:
        if definition.get('format')=='core-xml':
            import xml.etree.ElementTree as ET
            root=ET.fromstring(definition['xml_text'])
            for item in root.findall('./session_metadata/configuration'):
                if item.get('name')=='http_bridge_bindings_v1':
                    definition={'nodes':[dict(n,id=k) for k,n in json.loads(item.get('value','{}')).items()]}
        for n in definition.get('nodes') or []:
            if n.get('instance'):
                if not n.get('instance_version'): raise ValueError('节点引用必须固定模板版本: '+str(n.get('id')))
                refs.append((prefix+':'+str(n['id']),'template',n['instance'],n['instance_version']))
    return refs


def _sync_refs(c,collection,name,body,legacy=False):
    c.execute('DELETE FROM document_revision_refs WHERE collection=? AND name=?',(collection,name))
    c.execute('DELETE FROM reference_issues WHERE collection=? AND name=?',(collection,name))
    try: refs=_references(collection,body)
    except ValueError as e:
        if not legacy: raise
        refs=[];c.execute('INSERT OR REPLACE INTO reference_issues VALUES(?,?,?,?)',(collection,name,'unknown',str(e)))
    for slot,kind,target,version in refs:
        target=target or name
        exists=c.execute('SELECT 1 FROM revisions WHERE kind=? AND name=? AND version=?',(kind,target,version)).fetchone()
        if not exists:
            if not legacy and not (collection=='publication_drafts' and body.get('status')=='draft'): raise ValueError(f'版本引用不存在: {kind}/{target}/v{version}')
            c.execute('INSERT OR REPLACE INTO reference_issues VALUES(?,?,?,?)',(collection,name,slot,f'{kind}/{target}/v{version} 缺失；原记录保留'))
        else:
            c.execute('INSERT INTO document_revision_refs VALUES(?,?,?,?,?,?)',(collection,name,slot,kind,target,version))


def _revision_refs(c,kind,name,version,body,legacy=False):
    if kind!='template' or body.get('runtime_type')!='docker': return
    target,number=body.get('target_name'),body.get('target_version')
    if not target or not number or not c.execute("SELECT 1 FROM revisions WHERE kind='target' AND name=? AND version=?",(target,number)).fetchone():
        if legacy:
            c.execute('INSERT OR REPLACE INTO reference_issues VALUES(?,?,?,?)',('revisions',f'{kind}/{name}/{version}','target','旧模板缺少靶标版本；原记录保留'))
            return
        raise ValueError('模板必须引用存在的靶标发布版本')
    c.execute('INSERT OR IGNORE INTO revision_dependencies VALUES(?,?,?,?,?,?,?)',(kind,name,version,'target','target',target,number))


def now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def identifier(prefix):
    return prefix+'-'+uuid.uuid4().hex


def read(collection, name=None):
    with connection() as c:
        if name is not None:
            r = c.execute('SELECT body FROM documents WHERE collection=? AND name=?', (collection,name)).fetchone()
            return json.loads(r[0]) if r else None
        return {r['name']: json.loads(r['body']) for r in c.execute('SELECT name,body FROM documents WHERE collection=?', (collection,))}


def put(collection, name, body):
    with transaction() as c:
        c.execute('INSERT INTO documents VALUES(?,?,?) ON CONFLICT(collection,name) DO UPDATE SET body=excluded.body', (collection,name,json.dumps(body,ensure_ascii=False)))
        _sync_refs(c,collection,name,body)
    return body


def remove(collection,name):
    with connection() as c:
        c.execute('DELETE FROM documents WHERE collection=? AND name=?', (collection,name))


def migrate(collection, source):
    """Backup + count/content verification + switch authority in one transaction."""
    with transaction() as c, storage.lock(source):
        if c.execute('SELECT 1 FROM migrations WHERE source=?',(collection,)).fetchone():
            return
        path = Path(source)
        payload = storage.read_json(source) if path.exists() else {}
        backup = str(path)+'.pre-sqlite.bak'
        if path.exists() and not Path(backup).exists():
            shutil.copy2(path,backup)
        for name, body in payload.items():
            c.execute('INSERT OR IGNORE INTO documents VALUES(?,?,?)',(collection,name,json.dumps(body,ensure_ascii=False)))
            _sync_refs(c,collection,name,body,legacy=True)
        actual = {r['name']:json.loads(r['body']) for r in c.execute('SELECT name,body FROM documents WHERE collection=?',(collection,))}
        if any(actual.get(k) != v for k,v in payload.items()):
            raise ValueError('迁移校验失败: '+collection)
        issues=[]
        ids={}
        for name,body in payload.items():
            kind={'manifests':'target','instances':'template'}.get(collection,collection)
            ids[name]=str(uuid.uuid5(uuid.NAMESPACE_URL,kind+':'+name))
            if not isinstance(body,dict):
                issues.append({'name':name,'reason':'记录不是对象，保留原始内容'});continue
            if collection=='manifests':
                import delivery
                try: delivery.manifest(body)
                except ValueError as e: issues.append({'name':name,'reason':str(e)})
            elif collection=='instances':
                if body.get('runtime_type') not in ('docker','netns'):
                    issues.append({'name':name,'reason':'缺少有效 runtime_type'})
                if body.get('runtime_type')=='docker' and not body.get('image_tag'):
                    issues.append({'name':name,'reason':'缺少 image_tag，需补录绑定'})
            elif collection=='legacy_topologies':
                existing={r[0] for r in c.execute("SELECT name FROM documents WHERE collection='instances'")}
                for reference in body.get('instances') or []:
                    if reference not in existing: issues.append({'name':name,'reference':reference,'reason':'引用的节点模板不存在'})
        report={'count':len(payload),'verified':True,'entity_ids':ids,'issues':issues}
        c.execute('INSERT OR REPLACE INTO migration_reports VALUES(?,?)',(collection,json.dumps(report,ensure_ascii=False)))
        c.execute('INSERT INTO migrations VALUES(?,?,?,?)',(collection,backup,len(payload),now()))


def replace_collection(collection,payload):
    with transaction() as c:
        existing={r['name']:json.loads(r['body']) for r in c.execute('SELECT name,body FROM documents WHERE collection=?',(collection,))}
        for name in set(existing)-set(payload):
            c.execute('DELETE FROM documents WHERE collection=? AND name=?',(collection,name))
        for name,body in payload.items():
            if name not in existing or existing[name]!=body: put(collection,name,body)


def publish(kind,name,body):
    body = json.loads(json.dumps(body))
    body.pop('version',None)
    body.pop('entity_id',None)
    body.pop('publication_state',None)
    canonical = json.dumps(body,sort_keys=True,ensure_ascii=False)
    with transaction() as c:
        row = c.execute('SELECT version,body FROM revisions WHERE kind=? AND name=? ORDER BY version DESC LIMIT 1',(kind,name)).fetchone()
        if row:
            old = json.loads(row['body'])
            comparison = {k:v for k,v in old.items() if k not in ('version','entity_id','publication_state')}
            if json.dumps(comparison,sort_keys=True,ensure_ascii=False) == canonical:
                return {**old,'publication_state':'published'}
        version = row['version']+1 if row else 1
        body.update(version=version, entity_id=str(uuid.uuid5(uuid.NAMESPACE_URL,kind+':'+name)))
        c.execute('INSERT INTO revisions(kind,name,version,body,created_at) VALUES(?,?,?,?,?)',(kind,name,version,json.dumps(body,ensure_ascii=False),now()))
        _revision_refs(c,kind,name,version,body)
        return {**body,'publication_state':'published'}


def revision(kind,name,version=None):
    with connection() as c:
        sql='SELECT body,publication_state FROM revisions WHERE kind=? AND name=?'
        args=[kind,name]
        if version is not None:
            sql+=' AND version=?';args.append(version)
        r=c.execute(sql+' ORDER BY version DESC LIMIT 1',args).fetchone()
        return {**json.loads(r[0]),'publication_state':r['publication_state']} if r else None


def history(kind,name):
    with connection() as c:
        return [{**json.loads(r[0]),'publication_state':r['publication_state']} for r in c.execute('SELECT body,publication_state FROM revisions WHERE kind=? AND name=? ORDER BY version DESC',(kind,name))]


def references(kind,name,version=None):
    with connection() as c:
        suffix=' AND version=?' if version is not None else ''
        args=[kind,name]+([version] if version is not None else [])
        documents=[dict(r) for r in c.execute('SELECT collection,name,slot,version FROM document_revision_refs WHERE kind=? AND target_name=?'+suffix,args)]
        suffix=' AND target_version=?' if version is not None else ''
        revisions=[dict(r) for r in c.execute('SELECT owner_kind,owner_name,owner_version,target_version FROM revision_dependencies WHERE target_kind=? AND target_name=?'+suffix,args)]
    return {'documents':documents,'revisions':revisions}
