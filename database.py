"""Shared SQLite transactions and filesystem commit/rollback hooks per thread."""
import contextlib
import os
import sqlite3
import threading

_local = threading.local()


def current(path):
    entry = getattr(_local, 'entry', None)
    return entry if entry and entry['path'] == os.path.abspath(path) else None


def connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA foreign_keys=ON')
    return c


@contextlib.contextmanager
def connection(path):
    entry = current(path)
    if entry:
        yield entry['connection']
        return
    c = connect(path)
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


@contextlib.contextmanager
def transaction(path):
    if current(path):
        yield current(path)['connection']
        return
    c = connect(path)
    entry = {'path': os.path.abspath(path), 'connection': c, 'commit': [], 'rollback': []}
    c.execute('BEGIN IMMEDIATE')
    _local.entry = entry
    committed = False
    try:
        yield c
        c.commit()
        committed = True
    except BaseException:
        c.rollback()
        raise
    finally:
        _local.entry = None
        c.close()
        callbacks = entry['commit'] if committed else reversed(entry['rollback'])
        for callback in callbacks:
            callback()


def on_commit(path, callback):
    entry = current(path)
    if entry:
        entry['commit'].append(callback)
    else:
        callback()


def on_rollback(path, callback):
    entry = current(path)
    if entry:
        entry['rollback'].append(callback)
