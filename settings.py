"""Runtime configuration. Importing configuration never touches the filesystem."""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory


@dataclass
class Settings:
    core_address: str
    host: str
    port: int
    data_dir: Path
    db_path: Path
    services_dir: Path
    import_dir: Path
    temp_dir: Path

    @classmethod
    def from_env(cls):
        data = Path(os.environ.get("BRIDGE_DATA_DIR", "/opt/artifacts/core-drill"))
        return cls(
            os.environ.get("CORE_GRPC_ADDRESS", "localhost:50051"),
            os.environ.get("BRIDGE_HOST", "0.0.0.0"),
            int(os.environ.get("BRIDGE_PORT", "8080")),
            data,
            Path(os.environ.get("BRIDGE_DB_PATH", str(data / "platform.db"))),
            Path(os.environ.get("CORE_CUSTOM_SERVICES_DIR", "/opt/core/etc/custom_services")),
            Path(os.environ.get("BRIDGE_IMPORT_DIR", str(data / "image-imports"))),
            Path(os.environ.get("BRIDGE_TEMP_DIR", str(data / "tmp"))),
        )


settings = Settings.from_env()


@contextmanager
def request_file(suffix: str):
    """Shared host/daemon filesystem, unique per request and cleaned on completion."""
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="request-", dir=settings.temp_dir) as directory:
        path = Path(directory) / ("payload" + suffix)
        yield path
