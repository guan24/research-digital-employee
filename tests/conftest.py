from __future__ import annotations

from pathlib import Path
import pytest

from app.config import Settings
from app.infrastructure.database import Database


@pytest.fixture(autouse=True)
def offline_network_guard(monkeypatch):
    """Tests may use mocked transports and in-process ASGI, never live sockets."""
    import socket
    import ipaddress
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def offline_dns(host, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # Mocked HTTP clients still exercise the public-address validator.
            host = "127.0.0.1" if host == "localhost" else "93.184.216.34"
        return original_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(sock, address, *, original=original_connect):
        # Windows asyncio uses a loopback socket pair for its wakeup pipe.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original(sock, address)
        raise AssertionError("Offline test attempted a real network connection")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda sock, addr: guarded_connect(sock, addr, original=original_connect_ex))
    monkeypatch.setattr(socket, "getaddrinfo", offline_dns)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_home=tmp_path,
        database_path=tmp_path / "data" / "tasks.sqlite",
        checkpoint_path=tmp_path / "data" / "checkpoints.sqlite",
        workspace_dir=tmp_path / "workspaces",
        openai_api_key="test-key",
        openai_base_url=None,
        openai_model="test-model",
        model_timeout_seconds=5,
        tavily_api_key="",
        search_timeout_seconds=5,
        fetch_timeout_seconds=5,
        max_fetch_bytes=100_000,
        worker_poll_seconds=0.01,
        worker_heartbeat_seconds=0.01,
        worker_lease_seconds=1,
        retry_base_seconds=1,
        retry_max_seconds=2,
        default_max_attempts=3,
        default_max_steps=12,
        default_max_runtime_seconds=60,
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.init()
    return database
