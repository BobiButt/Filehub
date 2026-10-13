"""Relay: client→client chunk forwarding (§9.5).

Binary frames arrive from the sender, are CRC-verified, then forwarded
verbatim to the receiver's socket. Relay data NEVER touches the disk.
"""

from __future__ import annotations

import logging
import struct
import time
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import get_config
from app.core.broadcaster import broadcaster
from app.core.connection_manager import manager
from app.core.file_manager import BadPathError, file_manager
from app.core.hub import hub
from app.core.lane_scheduler import lane_scheduler
from app.models.schemas import DeviceState, TransferKind, TransferState, TransferStatus

logger = logging.getLogger(__name__)

# [4B id length][id utf-8][8B chunk index][4B crc32][payload]  (§7.3)
_LENGTH = struct.Struct(">I")
_INDEX = struct.Struct(">Q")
_CRC = struct.Struct(">I")
_MAX_ID_LEN = 64
_DONE_GRACE = 120.0


class RelayError(Exception):
    """Protocol violation during a relay (rendered as an error message)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_frame(data: bytes) -> tuple[str, int, int, bytes]:
    """Split a binary relay frame; raises RelayError when malformed."""
    if len(data) < _LENGTH.size:
        raise RelayError("frame too short")
    id_len = _LENGTH.unpack_from(data, 0)[0]
    if id_len == 0 or id_len > _MAX_ID_LEN:
        raise RelayError("bad transfer id length")
    header_end = _LENGTH.size + id_len + _INDEX.size + _CRC.size
    if len(data) < header_end:
        raise RelayError("frame header truncated")
    try:
        transfer_id = data[_LENGTH.size : _LENGTH.size + id_len].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RelayError("transfer id is not utf-8") from exc
    index = _INDEX.unpack_from(data, _LENGTH.size + id_len)[0]
    crc = _CRC.unpack_from(data, _LENGTH.size + id_len + _INDEX.size)[0]
    return transfer_id, index, crc, data[header_end:]


@dataclass
class RelaySession:
    """Server-side memory of one pending/active relay."""

    transfer_id: str
    sender_id: str
    receiver_id: str
    filename: str
    size: int
    sha256: str
    done_at: float | None = None


class RelayService:
    """Creates offers, forwards chunks, finishes/aborts transfers."""

    def __init__(self) -> None:
        self.config = get_config()
        self.relays: dict[str, RelaySession] = {}

    # ----------------------------------------------------------- helpers --

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _purge_done(self) -> None:
        now = time.monotonic()
        stale = [
            tid
            for tid, session in self.relays.items()
            if session.done_at is not None and now - session.done_at > _DONE_GRACE
        ]
        for tid in stale:
            self.relays.pop(tid, None)

    def _get(self, transfer_id: str) -> RelaySession:
        session = self.relays.get(transfer_id)
        if session is None:
            raise RelayError("unknown transfer")
        return session

    def _lanes_for(self, device_id: str) -> int:
        lanes = lane_scheduler.lanes_for(hub.user_count())
        active = sum(
            1
            for t in hub.transfers_for(device_id)
            if t.sender_id == device_id
        )
        return lanes - active

    # ------------------------------------------------------------ lifecycle --

    async def create_offer(
        self, from_id: str, to_id: str, filename: str, size: int, sha256: str
    ) -> str:
        """Register a relay offer and notify the receiver (§11.3 step 1-2)."""
        self._purge_done()
        sender = hub.get_device(from_id)
        receiver = hub.get_device(to_id)
        if (
            sender is None
            or receiver is None
            or sender.state != DeviceState.APPROVED
            or receiver.state != DeviceState.APPROVED
        ):
            raise RelayError("both devices must be online and approved")
        if from_id == to_id:
            raise RelayError("cannot send to yourself")
        if size < 0 or size > self.config.max_file_size:
            raise RelayError("file too large")
        sha = (sha256 or "").lower()
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise RelayError("invalid sha256")
        try:
            safe_name = file_manager.sanitize(filename)
        except BadPathError as exc:
            raise RelayError(str(exc)) from exc
        if self._lanes_for(from_id) <= 0:
            raise RelayError("no free transfer lanes, try again shortly")

        transfer_id = str(uuid.uuid4())
        state = TransferState(
            id=transfer_id,
            kind=TransferKind.RELAY,
            sender_id=from_id,
            receiver_id=to_id,
            filename=safe_name,
            size=size,
            chunks_total=(size + self.config.chunk_size - 1)
            // self.config.chunk_size,
            status=TransferStatus.PENDING,
            started_at=self._now(),
        )
        session = RelaySession(
            transfer_id=transfer_id,
            sender_id=from_id,
            receiver_id=to_id,
            filename=safe_name,
            size=size,
            sha256=sha,
        )
        self.relays[transfer_id] = session
        hub.create_transfer(state)
        logger.info("Relay offer %s: %r -> %r", transfer_id, sender.name, receiver.name)
        await broadcaster.relay_offer(state, sender, sha)
        await broadcaster.log(
            TransferKind.RELAY,
            sender.name,
            receiver.name,
            "started",
            filename=safe_name,
            size=size,
            transfer_id=transfer_id,
        )
        return transfer_id

    async def accept(self, transfer_id: str, by_id: str) -> None:
        """Receiver is ready → sender may start streaming (§11.3 step 3-4)."""
        session = self._get(transfer_id)
        if by_id != session.receiver_id:
            raise RelayError("only the receiver can accept")
        state = hub.get_transfer(transfer_id)
        if state is None or state.status != TransferStatus.PENDING:
            raise RelayError("transfer is not awaiting an answer")
        state.status = TransferStatus.ACTIVE
        receiver = hub.get_device(by_id)
        await broadcaster.relay_accepted(
            transfer_id, receiver.name if receiver else "", session.sender_id
        )
        await broadcaster.transfer_start(state)  # push ACTIVE status to dashboards

    async def reject(self, transfer_id: str, by_id: str, reason: str = "declined") -> None:
        """Receiver declines → sender is told, dashboards see it (step 3)."""
        session = self._get(transfer_id)
        if by_id != session.receiver_id:
            raise RelayError("only the receiver can decline")
        state = hub.get_transfer(transfer_id)
        if state is None or state.status != TransferStatus.PENDING:
            raise RelayError("transfer is not awaiting an answer")
        state.status = TransferStatus.REJECTED
        state.finished_at = self._now()
        state.error = reason
        sender = hub.get_device(session.sender_id)
        receiver = hub.get_device(session.receiver_id)
        await broadcaster.relay_rejected(transfer_id, reason, session.sender_id)
        await broadcaster.transfer_done(state)
        await broadcaster.log(
            TransferKind.RELAY,
            receiver.name if receiver else "?",
            sender.name if sender else "?",
            "declined",
            filename=session.filename,
            size=session.size,
            transfer_id=transfer_id,
        )
        self.relays.pop(transfer_id, None)

    async def finish(self, transfer_id: str, by_id: str) -> None:
        """Sender says every chunk was sent → mark DONE (§11.3 step 8)."""
        session = self._get(transfer_id)
        if by_id != session.sender_id:
            raise RelayError("only the sender can finish")
        state = hub.get_transfer(transfer_id)
        if state is None or state.status != TransferStatus.ACTIVE:
            raise RelayError("transfer is not active")
        state.status = TransferStatus.DONE
        state.bytes_transferred = state.size
        state.chunks_received = state.chunks_total
        state.finished_at = self._now()
        session.done_at = time.monotonic()
        sender = hub.get_device(session.sender_id)
        receiver = hub.get_device(session.receiver_id)
        await broadcaster.relay_finished(transfer_id, session.receiver_id)
        await broadcaster.transfer_done(state)
        await broadcaster.log(
            TransferKind.RELAY,
            sender.name if sender else "?",
            receiver.name if receiver else "?",
            "done",
            filename=session.filename,
            size=session.size,
            transfer_id=transfer_id,
        )

    # ------------------------------------------------------------- chunks --

    async def forward_chunk(self, raw: bytes, from_id: str) -> None:
        """Verify one binary frame and relay it verbatim to the receiver."""
        transfer_id, index, crc, payload = parse_frame(raw)
        session = self._get(transfer_id)
        if from_id != session.sender_id:
            raise RelayError("you are not the sender of this transfer")
        state = hub.get_transfer(transfer_id)
        if state is None or state.status != TransferStatus.ACTIVE:
            raise RelayError("transfer is not active")
        if len(payload) > self.config.chunk_size:
            await self.abort(transfer_id, "chunk larger than chunk_size")
            return
        if index >= max(1, state.chunks_total) and session.size > 0:
            await self.abort(transfer_id, "chunk index out of range")
            return
        if (zlib.crc32(payload) & 0xFFFFFFFF) != crc:
            # TCP already protects framing; a bad CRC means real corruption.
            await self.abort(transfer_id, "crc32 mismatch")
            return
        state.bytes_transferred += len(payload)
        state.chunks_received += 1
        hub.touch_transfer(transfer_id)
        hub.record_progress(transfer_id, state.bytes_transferred)
        delivered = await manager.send_bytes(session.receiver_id, raw)
        if not delivered:
            await self.abort(transfer_id, "receiver disconnected")
            return
        await broadcaster.transfer_progress(transfer_id)

    # ---------------------------------------------------------- teardown --

    async def abort(self, transfer_id: str, reason: str) -> None:
        """Tear down a relay and notify both endpoints + dashboards."""
        session = self.relays.pop(transfer_id, None)
        state = hub.get_transfer(transfer_id)
        endpoints = [session.sender_id, session.receiver_id] if session else []
        if state is None:
            return
        if state.status in (
            TransferStatus.DONE,
            TransferStatus.FAILED,
            TransferStatus.REJECTED,
            TransferStatus.CANCELLED,
        ):
            return  # already terminal (e.g. finish raced an abort)
        sender = hub.get_device(state.sender_id)
        receiver = hub.get_device(state.receiver_id)
        logger.info("Aborting relay %s: %s", transfer_id, reason)
        if endpoints:
            await broadcaster.relay_aborted(transfer_id, reason, endpoints)
        await broadcaster.transfer_failed(transfer_id, reason)
        await broadcaster.log(
            TransferKind.RELAY,
            sender.name if sender else "?",
            receiver.name if receiver else "?",
            f"failed: {reason}",
            filename=state.filename,
            size=state.size,
            transfer_id=transfer_id,
        )

    async def cancel(self, transfer_id: str, by_id: str, reason: str = "cancelled") -> None:
        """cancel_transfer (§7.2) — either endpoint may cancel."""
        session = self.relays.get(transfer_id)
        state = hub.get_transfer(transfer_id)
        if session is None and state is None:
            raise RelayError("unknown transfer")
        if session is not None and by_id not in (session.sender_id, session.receiver_id):
            raise RelayError("you are not part of this transfer")
        if session is None and state is not None:
            if by_id not in (state.sender_id, state.receiver_id):
                raise RelayError("you are not part of this transfer")
            if state.status == TransferStatus.DONE:
                # Receiver found a SHA-256 mismatch after the sender finished:
                # flip the visible outcome to failed on every dashboard (§13).
                await broadcaster.transfer_failed(transfer_id, f"failed: {reason}")
                await broadcaster.log(
                    TransferKind.RELAY,
                    hub.get_device(by_id).name if hub.get_device(by_id) else "?",
                    "SERVER",
                    f"failed: {reason}",
                    filename=state.filename,
                    size=state.size,
                    transfer_id=transfer_id,
                )
                return
            raise RelayError("transfer is not active")
        await self.abort(transfer_id, reason)

    async def abort_for_device(self, device_id: str, reason: str) -> None:
        """Abort every relay this device takes part in (§9.5 / §11.3)."""
        affected = [
            tid
            for tid, session in list(self.relays.items())
            if device_id in (session.sender_id, session.receiver_id)
        ]
        for transfer_id in affected:
            await self.abort(transfer_id, reason)


# Process-wide singleton.
relay_service = RelayService()
