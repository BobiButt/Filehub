"""Server configuration (pydantic-settings).

All tunables live here. Values can be overridden with environment variables
or a `.env` file in the project root (e.g. ``PORT=9000 python run.py``).
"""

from __future__ import annotations

import socket
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerConfig(BaseSettings):
    """Runtime configuration for the FileHub server."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8000
    shared_dir: Path = Path("./shared")
    tmp_dir: Path = Path("./tmp")
    sessions_dir: Path = Path("./sessions")

    chunk_size: int = 1_048_576  # 1 MB
    max_file_size: int = 20 * 1024**3  # 20 GB

    global_lane_budget: int = 18
    per_user_lanes_small: int = 4  # 1-3 users online
    per_user_lanes_medium: int = 3  # 4-6 users
    per_user_lanes_large: int = 2  # 7-10 users
    per_user_lanes_max: int = 1  # 11+ users

    join_code_length: int = 6
    join_code_expires_hours: int = 0  # 0 = never, host rotates manually
    wrong_code_attempts_before_cooldown: int = 3
    cooldown_seconds: int = 30

    require_host_approval: bool = True
    auto_approve_first_n: int = 0  # 0 = approve all manually

    heartbeat_interval_seconds: int = 20
    heartbeat_timeout_seconds: int = 60

    persist_trusted_devices: bool = True  # SQLite for trusted fingerprints only
    log_ips: bool = False  # never persist IPs

    trusted_db_path: Path = Path("./trusted_devices.db")


@lru_cache
def get_config() -> ServerConfig:
    """Return the process-wide configuration singleton."""
    return ServerConfig()


def get_lan_ip() -> str:
    """Best-effort local LAN IP of this machine (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    if ip.startswith("127."):
        # No default route: fall back to whatever the hostname resolves to.
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                candidate = info[4][0]
                if not candidate.startswith("127."):
                    return candidate
        except OSError:
            pass
    return ip


def get_urls(config: ServerConfig | None = None) -> list[str]:
    """All candidate browser URLs for this server (localhost + LAN IPs)."""
    cfg = config or get_config()
    urls = [f"http://127.0.0.1:{cfg.port}", f"http://localhost:{cfg.port}"]
    lan_ip = get_lan_ip()
    lan_url = f"http://{lan_ip}:{cfg.port}"
    if lan_ip != "127.0.0.1" and lan_url not in urls:
        urls.append(lan_url)
    return urls
