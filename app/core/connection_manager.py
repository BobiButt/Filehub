"""Live WebSocket clients + broadcast primitives (§9.2).

One socket per device. All sends are awaited so TCP backpressure flows
back to senders — nothing is fire-and-forget.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Iterable

from starlette.websockets import WebSocket

from app.config import ServerConfig, get_config

if TYPE_CHECKING:
    from app.models.schemas import DeviceInfo

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Owns the device_id → WebSocket map and low-level sending."""

    def __init__(self, config: ServerConfig | None = None) -> None:
        self.config = config or get_config()
        self.sockets: dict[str, WebSocket] = {}
        self.last_seen: dict[str, float] = {}

    # ---------------------------------------------------------- lifecycle --

    async def connect(self, ws: WebSocket, device: "DeviceInfo") -> None:
        """Attach a socket to a device (replaces any previous socket)."""
        previous = self.sockets.get(device.id)
        if previous is not None and previous is not ws:
            try:
                await previous.close()
            except Exception:  # noqa: BLE001 - old socket already dead
                pass
        await ws.accept()
        self.sockets[device.id] = ws
        self.touch(device.id)
        logger.debug("WS connected for %r", device.name)

    async def disconnect(self, device_id: str, ws: WebSocket | None = None) -> bool:
        """Detach a socket. Returns True if this call removed it.

        ``ws`` guards against a stale socket closing after a replacement
        (e.g. page reload) has already reconnected.
        """
        current = self.sockets.get(device_id)
        if current is None:
            return False
        if ws is not None and current is not ws:
            return False
        self.sockets.pop(device_id, None)
        self.last_seen.pop(device_id, None)
        try:
            await current.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
        return True

    def is_connected(self, device_id: str) -> bool:
        return device_id in self.sockets

    def touch(self, device_id: str) -> None:
        self.last_seen[device_id] = time.monotonic()

    def seconds_since_message(self, device_id: str) -> float:
        seen = self.last_seen.get(device_id)
        if seen is None:
            return float("inf")
        return time.monotonic() - seen

    # -------------------------------------------------------------- send --

    @staticmethod
    def _dump(message: dict[str, Any]) -> str:
        return json.dumps(message, separators=(",", ":"))

    async def send_to(self, device_id: str, message: dict[str, Any]) -> bool:
        """Send a JSON message to one device; False if it is gone/dead."""
        ws = self.sockets.get(device_id)
        if ws is None:
            return False
        try:
            await ws.send_text(self._dump(message))
            return True
        except Exception:  # noqa: BLE001 - socket died mid-send
            logger.debug("send_to failed for %s", device_id)
            await self.disconnect(device_id, ws)
            return False

    async def send_bytes(self, device_id: str, data: bytes) -> bool:
        """Send a binary frame to one device; False if it is gone/dead."""
        ws = self.sockets.get(device_id)
        if ws is None:
            return False
        try:
            await ws.send_bytes(data)
            return True
        except Exception:  # noqa: BLE001 - socket died mid-send
            logger.debug("send_bytes failed for %s", device_id)
            await self.disconnect(device_id, ws)
            return False

    async def broadcast(
        self,
        message: dict[str, Any],
        roles: Iterable[str] | None = None,
        device_ids: Iterable[str] | None = None,
    ) -> None:
        """Send to all connected devices (optionally filtered by role)."""
        from app.core.hub import hub  # local import avoids cycles at module load

        allowed_roles = set(roles) if roles is not None else None
        explicit = set(device_ids) if device_ids is not None else None
        payload = self._dump(message)
        for device_id, ws in list(self.sockets.items()):
            if explicit is not None and device_id not in explicit:
                continue
            if allowed_roles is not None:
                device = hub.get_device(device_id)
                if device is None or device.role not in allowed_roles:
                    continue
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001 - socket died mid-send
                await self.disconnect(device_id, ws)

    async def send_pings(self) -> None:
        """App-level heartbeat ping (browsers answer with a 'pong' message)."""
        await self.broadcast({"type": "ping", "payload": None})


# Process-wide singleton.
manager = ConnectionManager()
