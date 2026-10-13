#!/usr/bin/env python3
"""FileHub entry point.

Starts the uvicorn server and prints everything you need in the terminal:
server URL(s), the join code, the host admin URL, and a QR code.

On startup, if the configured port is already in use by another
process, that process is terminated automatically so FileHub can bind.
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

import qrcode
import uvicorn

# Allow `python run.py` from any working directory.
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_config, get_lan_ip  # noqa: E402
from app.core.hub import hub  # noqa: E402
from app.main import app  # noqa: E402


def _port_in_use(host: str, port: int) -> bool:
    """Return True if something is already listening on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return True
            raise
    return False


def _pids_on_port(port: int) -> list[int]:
    """Find PIDs listening on the given TCP port. Linux + macOS only."""
    pids: set[int] = set()

    # Primary: `lsof -ti tcp:PORT` — available on most Linux/macOS setups.
    try:
        import subprocess

        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        for line in out.stdout.split():
            if line.strip().isdigit():
                pids.add(int(line.strip()))
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    # Fallback: parse /proc/net/tcp + /proc/*/fd (Linux only, no deps).
    if not pids and sys.platform.startswith("linux"):
        try:
            import glob
            import re

            inodes: set[str] = set()
            port_hex = f"{port:04X}"
            for proc_file in ("/proc/net/tcp", "/proc/net/tcp6"):
                try:
                    with open(proc_file) as fh:
                        next(fh)  # header
                        for line in fh:
                            parts = line.split()
                            if len(parts) < 10:
                                continue
                            local = parts[1]
                            if ":" not in local:
                                continue
                            _, lport = local.rsplit(":", 1)
                            if lport.upper() == port_hex and parts[3] == "0A":
                                inodes.add(parts[9])
                except OSError:
                    continue
            if inodes:
                for fd_path in glob.glob("/proc/[0-9]*/fd/*"):
                    try:
                        target = os.readlink(fd_path)
                    except OSError:
                        continue
                    m = re.match(r"socket:\[(\d+)\]", target)
                    if m and m.group(1) in inodes:
                        pid_str = fd_path.split("/")[2]
                        if pid_str.isdigit():
                            pids.add(int(pid_str))
        except Exception:  # noqa: BLE001 — never let cleanup crash startup
            pass

    return sorted(pids)


def _free_port(port: int) -> None:
    """If port is in use, kill the holder. Never kill our own PID."""
    if not _port_in_use("0.0.0.0", port):
        return

    pids = _pids_on_port(port)
    my_pid = os.getpid()
    targets = [p for p in pids if p != my_pid]

    if not targets:
        print(
            f"⚠  Port {port} is in use but no PID could be identified. "
            f"Run:  sudo fuser -k {port}/tcp",
            file=sys.stderr,
        )
        return

    print(f"⚠  Port {port} is in use by PID(s): {targets}. Terminating…")

    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            print(
                f"   Cannot kill PID {pid} (permission denied). "
                f"Run:  sudo kill {pid}",
                file=sys.stderr,
            )

    # Wait up to 3 seconds for the socket to free.
    for _ in range(30):
        if not _port_in_use("0.0.0.0", port):
            print(f"   Port {port} is now free.")
            return
        time.sleep(0.1)

    # Still held → escalate to SIGKILL.
    for pid in targets:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass

    for _ in range(20):
        if not _port_in_use("0.0.0.0", port):
            print(f"   Port {port} is now free (after SIGKILL).")
            return
        time.sleep(0.1)

    print(
        f"⚠  Could not free port {port}. Try:  sudo fuser -k {port}/tcp",
        file=sys.stderr,
    )


def main() -> None:
    config = get_config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # ---------------------------------------------------------- port guard
    _free_port(config.port)

    lan_ip = get_lan_ip()
    lan_url = (
        "http://127.0.0.1"
        if lan_ip.startswith("127.")
        else f"http://{lan_ip}"
    )
    primary_url = f"{lan_url}:{config.port}/"
    admin_url = f"{primary_url}?admin={hub.admin_token}"

    line = "=" * 62
    print(line)
    print("  FileHub — LAN file transfer hub")
    print(line)
    print(f"  Open in a browser : {primary_url}")
    if not lan_url.endswith("127.0.0.1"):
        print(f"  Local only        : http://127.0.0.1:{config.port}/")
    print(f"  Join code         : {hub.join_code}")
    print(f"  Host admin URL    : {admin_url}")
    print(f"  Shared folder     : {config.shared_dir.resolve()}")
    print("  Guests: open the URL, enter the join code, wait for approval.")
    print("  Keep this terminal running while others connect.")
    print(line)
    print("  Scan to join (same WiFi/hotspot required):")
    qr = qrcode.QRCode(border=1)
    qr.add_data(primary_url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    print(line)

    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level="warning",
        ws="websockets",
    )


if __name__ == "__main__":
    main()