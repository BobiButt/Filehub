"""Hub: the single source of truth for devices, transfers and traffic.

Everything mutable that the whole app shares lives here (§9.1). IPs are
kept on DeviceInfo in RAM for the session only and are never persisted.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import ServerConfig, get_config
from app.core.file_manager import file_manager
from app.core.lane_scheduler import lane_scheduler
from app.core.security import (
    detect_os,
    generate_admin_token,
    generate_join_code,
)
from app.models.schemas import (
    DeviceInfo,
    DeviceState,
    TrafficEvent,
    TransferState,
    TransferStatus,
)

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {
    TransferStatus.DONE,
    TransferStatus.FAILED,
    TransferStatus.REJECTED,
    TransferStatus.CANCELLED,
}

# How long finished transfers stay in RAM (for late UI events / SHA retry).
_FINALIZED_MAX_AGE = 120.0


@dataclass
class DeviceRuntime:
    """Per-connection bookkeeping that is not part of DeviceInfo."""

    ws_connected: bool = False
    announced: bool = False
    pending_announced: bool = False
    offline_at: float | None = None


@dataclass
class _SpeedSample:
    ts: float
    total: int


class Hub:
    """Owns devices, transfers, traffic, join code and admin token."""

    def __init__(self, config: ServerConfig | None = None) -> None:
        self.config = config or get_config()
        self.devices: dict[str, DeviceInfo] = {}
        self.transfers: dict[str, TransferState] = {}
        self.traffic: deque[TrafficEvent] = deque(maxlen=200)
        self.join_code: str = generate_join_code(self.config.join_code_length)
        self.admin_token: str = generate_admin_token()
        self._runtime: dict[str, DeviceRuntime] = {}
        self._speeds: dict[str, deque[_SpeedSample]] = {}
        self._last_progress_at: dict[str, float] = {}
        self._transfer_activity: dict[str, float] = {}
        self._transfer_finalized_at: dict[str, float] = {}

    # ------------------------------------------------------------- devices --

    def register_device(
        self,
        *,
        name: str,
        user_agent: str,
        fingerprint: str,
        ip: str,
        role: str,
        state: DeviceState,
        device_id: str | None = None,
        ws: object = None,  # noqa: ARG002 - kept for API parity with §9.1
    ) -> DeviceInfo:
        """Create a DeviceInfo and track it. The id is always server-generated."""
        now = datetime.now(timezone.utc)
        device = DeviceInfo(
            id=device_id or str(uuid.uuid4()),
            name=name,
            os="Unknown",
            user_agent=user_agent,
            fingerprint=fingerprint,
            ip=ip,
            state=state,
            role=role,  # type: ignore[arg-type]
            connected_at=now,
            last_seen=now,
        )
        device.os = detect_os(user_agent)
        self.devices[device.id] = device
        self._runtime[device.id] = DeviceRuntime()
        logger.info("Registered device %r (%s, %s)", device.name, device.role, device.os)
        return device

    def unregister_device(self, device_id: str) -> DeviceInfo | None:
        """Drop a device completely; returns it if it existed."""
        self._runtime.pop(device_id, None)
        device = self.devices.pop(device_id, None)
        if device:
            logger.info("Unregistered device %r", device.name)
        return device

    def get_device(self, device_id: str) -> DeviceInfo | None:
        return self.devices.get(device_id)

    def runtime(self, device_id: str) -> DeviceRuntime:
        return self._runtime.setdefault(device_id, DeviceRuntime())

    def set_device_state(self, device_id: str, state: DeviceState) -> DeviceInfo | None:
        device = self.devices.get(device_id)
        if device is None:
            return None
        device.state = state
        device.last_seen = datetime.now(timezone.utc)
        return device

    def touch_device(self, device_id: str) -> None:
        device = self.devices.get(device_id)
        if device is not None:
            device.last_seen = datetime.now(timezone.utc)

    def approved_devices(self) -> list[DeviceInfo]:
        return [d for d in self.devices.values() if d.state == DeviceState.APPROVED]

    def pending_devices(self) -> list[DeviceInfo]:
        return [d for d in self.devices.values() if d.state == DeviceState.PENDING]

    def user_count(self) -> int:
        """Approved users online (the scheduler's input)."""
        return len(self.approved_devices())

    # ------------------------------------------------------------- traffic --

    def add_traffic(self, event: TrafficEvent) -> None:
        """Append to the in-RAM traffic log (capped at 200, wiped on restart)."""
        self.traffic.append(event)

    def recent_traffic(self, limit: int = 50) -> list[TrafficEvent]:
        return list(self.traffic)[-limit:]

    # ----------------------------------------------------------- transfers --

    def create_transfer(self, state: TransferState) -> TransferState:
        self.transfers[state.id] = state
        self._speeds[state.id] = deque()
        self._transfer_activity[state.id] = time.monotonic()
        return state

    def get_transfer(self, transfer_id: str) -> TransferState | None:
        return self.transfers.get(transfer_id)

    def remove_transfer(self, transfer_id: str) -> None:
        self.transfers.pop(transfer_id, None)
        self._speeds.pop(transfer_id, None)
        self._last_progress_at.pop(transfer_id, None)
        self._transfer_activity.pop(transfer_id, None)
        self._transfer_finalized_at.pop(transfer_id, None)

    def finalize_transfer(self, state: TransferState) -> None:
        """Mark a transfer terminal; it lingers briefly, then is pruned."""
        self.transfers[state.id] = state
        self._transfer_finalized_at[state.id] = time.monotonic()

    def active_transfers(self) -> list[TransferState]:
        return [t for t in self.transfers.values() if t.status not in TERMINAL_STATUSES]

    def active_stream_count(self) -> int:
        return sum(
            1 for t in self.transfers.values() if t.status == TransferStatus.ACTIVE
        )

    def transfers_for(self, device_id: str) -> list[TransferState]:
        """Non-terminal transfers where the device is an endpoint."""
        return [
            t
            for t in self.active_transfers()
            if device_id in (t.sender_id, t.receiver_id)
        ]

    def prune_finalized(self) -> None:
        """Drop finished transfers after a short grace period."""
        now = time.monotonic()
        stale = [
            tid
            for tid, ts in self._transfer_finalized_at.items()
            if now - ts > _FINALIZED_MAX_AGE
        ]
        for tid in stale:
            self.remove_transfer(tid)

    # -------------------------------------------------------------- speed --

    def record_progress(self, transfer_id: str, total_bytes: int) -> None:
        """Feed the rolling 3 s speed window with the new cumulative total."""
        samples = self._speeds.get(transfer_id)
        if samples is None:
            samples = self._speeds[transfer_id] = deque()
        now = time.monotonic()
        samples.append(_SpeedSample(ts=now, total=total_bytes))
        cutoff = now - 4.0
        while len(samples) > 2 and samples[0].ts < cutoff:
            samples.popleft()

    def speed_for(self, transfer_id: str) -> int:
        """Rolling average bytes/second over the last ~3 seconds."""
        samples = self._speeds.get(transfer_id)
        if not samples or len(samples) < 2:
            return 0
        now = time.monotonic()
        window = [s for s in samples if now - s.ts <= 3.0]
        if len(window) < 2:
            return 0
        first, last = window[0], window[-1]
        span = last.ts - first.ts
        if span <= 0:
            return 0
        return max(0, int((last.total - first.total) / span))

    def should_broadcast_progress(self, transfer_id: str) -> bool:
        """Throttle progress broadcasts to every 250 ms per transfer (§7.4)."""
        now = time.monotonic()
        last = self._last_progress_at.get(transfer_id, 0.0)
        if now - last < 0.25:
            return False
        self._last_progress_at[transfer_id] = now
        return True

    def mark_progress_broadcast(self, transfer_id: str) -> None:
        self._last_progress_at[transfer_id] = time.monotonic()

    def touch_transfer(self, transfer_id: str) -> None:
        self._transfer_activity[transfer_id] = time.monotonic()

    def transfer_idle(self, transfer_id: str) -> float:
        """Seconds since the transfer last made progress."""
        return time.monotonic() - self._transfer_activity.get(
            transfer_id, time.monotonic()
        )

    # -------------------------------------------------------------- misc --

    def rotate_join_code(self) -> str:
        self.join_code = generate_join_code(self.config.join_code_length)
        return self.join_code

    def snapshot(self, role: str, device_id: str) -> dict:
        """Role-filtered dashboard snapshot (§7.1)."""
        self.prune_finalized()
        you = self.get_device(device_id)
        member = you is not None and you.state == DeviceState.APPROVED
        approved = self.approved_devices()
        devices = [d.model_dump(mode="json") for d in approved if d.id != device_id]
        pending = (
            [d.model_dump(mode="json") for d in self.pending_devices()]
            if role == "host"
            else []
        )
        return {
            "you": {
                "id": you.id if you else device_id,
                "name": you.name if you else "",
                "role": role,
                "state": you.state.value if you else DeviceState.PENDING.value,
            },
            "devices": devices,
            "pending": pending,
            "files": [
                f.model_dump(mode="json") for f in file_manager.list_files()
            ]
            if member
            else [],
            "traffic": [
                t.model_dump(mode="json") for t in self.recent_traffic(50)
            ]
            if member
            else [],
            "transfers": [
                t.model_dump(mode="json") for t in self.active_transfers()
            ]
            if member
            else [],
            "scheduler": {
                "global_budget": self.config.global_lane_budget,
                "active_streams": self.active_stream_count(),
                "your_lanes": lane_scheduler.lanes_for(len(approved)) if member else 0,
            },
        }


# Process-wide singleton.
hub = Hub()
