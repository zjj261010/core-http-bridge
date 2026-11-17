"""Database invariants for the remaining plan, independent of a live daemon."""
import concurrent.futures
import sqlite3
import pytest
import repository as repo
import platform_db as db


def test_one_active_identity_for_concurrent_requests():
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        values=list(executor.map(lambda _:db.ensure_drill(871,'epoch:one-session'),range(16)))
    assert len(set(values))==1
    with repo.connection() as c:
        assert c.execute("SELECT count(*) FROM drills WHERE core_sid=871 AND status='active'").fetchone()[0]==1
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO drills(drill_id,core_sid,status) VALUES('duplicate',871,'active')")


def test_publication_document_and_references_rollback_together():
    old=repo.publish('template','atomic',{'runtime_type':'netns','model':'bare','services':[]})
    repo.put('instances','atomic',old)
    with pytest.raises(ValueError):
        with repo.transaction():
            new=repo.publish('template','atomic',{'runtime_type':'netns','model':'bare','services':[{'name':'SSH'}]})
            repo.put('instances','atomic',new)
            repo.put('topologies','bad',{'definition':{'nodes':[{'id':1,'instance':'absent','instance_version':1}]}})
    assert repo.revision('template','atomic')['version']==1
    assert repo.read('instances','atomic')['version']==1
    assert repo.read('topologies','bad') is None


def test_published_revisions_are_immutable_in_database():
    repo.publish('template','immutable',{'runtime_type':'netns','services':[]})
    for statement in ("UPDATE revisions SET body='{}' WHERE name='immutable'", "DELETE FROM revisions WHERE name='immutable'"):
        with pytest.raises(sqlite3.IntegrityError):
            with repo.transaction() as c:c.execute(statement)


def test_live_reference_rejects_missing_version_and_enforces_node_uniqueness():
    drill=db.ensure_drill(872,'epoch:reference-session')
    version=repo.publish('template','node-template',{'runtime_type':'netns','services':[]})
    with pytest.raises(sqlite3.IntegrityError): db.register_deploy_ref(drill,872,1,'missing',1)
    db.register_deploy_ref(drill,872,1,'node-template',version['version'])
    db.register_deploy_ref(drill,872,1,'node-template',version['version'])
    with repo.connection() as c:
        assert c.execute('SELECT count(*) FROM deploy_refs WHERE core_sid=872 AND node_id=1').fetchone()[0]==1
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_workspace_and_frozen_history_keep_independent_references():
    first=repo.publish('template','history',{'runtime_type':'netns','model':'PC','services':[{'name':'DefaultRoute'}]})
    second=repo.publish('template','history',{'runtime_type':'netns','model':'host','services':[{'name':'SSH'}]})
    repo.put('deployments','old',{'drill_id':'old-drill','status':'closed','snapshot':{'nodes':[{'id':1,'instance':'history','instance_version':first['version']}]},'bindings':{}})
    repo.put('session_workspaces','1',{'bindings':{'1':{'instance':'history','instance_version':second['version']}}})
    assert repo.references('template','history',1)['documents'][0]['collection']=='deployments'
    assert repo.references('template','history',2)['documents'][0]['collection']=='session_workspaces'
