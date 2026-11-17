"""Atomic compatibility JSON storage and per-store read/modify/write locks."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def lock(path):
    with _guard:
        return _locks.setdefault(str(Path(path).absolute()), threading.RLock())


def read_json(path):
    with lock(path):
        try:
            with open(path, encoding="utf-8") as f:
                value = json.load(f)
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"存储格式错误，原文件保留: {path}")
        return value


def write_json(path, value):
    path = Path(path)
    with lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
