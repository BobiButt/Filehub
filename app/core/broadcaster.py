"""Broadcaster: thin layer over ConnectionManager that pushes WS events
to all connected dashboards (§9.8).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core import lane_scheduler as _lane_scheduler_mod
from app.core.connection_manager import manager
from app.core.file_manager import file_manager
from app.core.hub import hub
from app.models.schemas import (
    DeviceInfo,
    FileInfo,
    TrafficEvent,
    TransferKind,
    TransferState,
    TransferStatus,
)


class Broadcaster:
    """Builds WS envelopes and delivers them with the right audience."""

    # ------------------------------------------------------------ presence --

    async def _emit(
        self,
        event_type: str,
        payload: Any,
        exclude: str | None = None,
    ) -> None:
        """Send to connected, approved devices (optionally excluding one).

        Pending devices only ever receive their own snapshot — members-only
        events (files, traffic, transfers, presence) never leak to them.
        """
        ids = [d.id for d in hub.approved_devices() if d.id != exclude]
        await manager.broadcast({"type": event_type, "payload": payload}, device_ids=ids)

    async def device_pending(self, device: DeviceInfo) -> None:
        """New device waiting for approval — hosts only (§7.1)."""
        host_ids = [
            d.id
            for d in hub.approved_devices()
            if d.role == "host"
        ]
        await manager.broadcast(
            {"type": "device_pending", "payload": device.model_dump(mode="json")},
            device_ids=host_ids,
        )

    async def device_joined(self, device: DeviceInfo, exclude: str | None = None) -> None:
        await self._emit("device_joined", device.model_dump(mode="json"), exclude=exclude)

    async def device_left(self, device_id: str) -> None:
        await self._emit("device_left", {"device_id": device_id})

    async def device_kicked(self, device_id: str, reason: str) -> None:
        await self._emit("device_kicked", {"device_id": device_id, "reason": reason})

    async def snapshot_to(self, device_id: str) -> None:
        """Refresh one device's whole dashboard (e.g. right after approval)."""
        device = hub.get_device(device_id)
        if device is None:
            return
        await manager.send_to(
            device_id,
            {"type": "snapshot", "payload": hub.snapshot(device.role, device.id)},
        )

    async def error_to(self, device_id: str, message: str, context: str = "") -> None:
        payload: dict[str, Any] = {"error": message}
        if context:
            payload["context"] = context
        await manager.send_to(device_id, {"type": "error", "payload": payload})

    # --------------------------------------------------------------- files --

    async def files_changed(self, files: list[FileInfo] | None = None) -> None:
        listing = files if files is not None else file_manager.list_files()
        await self._emit(
            "files_changed", {"files": [f.model_dump(mode="json") for f in listing]}
        )

    # ------------------------------------------------------------- traffic --

    async def traffic(self, event: TrafficEvent) -> None:
        """Store a traffic event and push it to every approved dashboard."""
        hub.add_traffic(event)
        await self._emit("traffic", event.model_dump(mode="json"))

    async def log(
        self,
        kind: TransferKind | str,
        actor: str,
        target: str,
        status: str,
        filename: str | None = None,
        size: int | None = None,
        transfer_id: str | None = None,
    ) -> TrafficEvent:
        event = TrafficEvent(
            ts=datetime.now(timezone.utc),
            kind=kind,  # type: ignore[arg-type]
            actor=actor,
            target=target,
            filename=filename,
            size=size,
            status=status,
            transfer_id=transfer_id,
        )
        await self.traffic(event)
        return event

    # ----------------------------------------------------------- transfers --

    async def transfer_start(self, state: TransferState) -> None:
        await self._emit("transfer_start", state.model_dump(mode="json"))
        await self.scheduler_update()

    async def transfer_progress(self, transfer_id: str, force: bool = False) -> None:
        """Throttled progress push (250 ms per transfer, §7.4)."""
        state = hub.get_transfer(transfer_id)
        if state is None or state.status in (
            TransferStatus.DONE,
            TransferStatus.FAILED,
            TransferStatus.REJECTED,
            TransferStatus.CANCELLED,
        ):
            return
        if not force and not hub.should_broadcast_progress(transfer_id):
            return
        hub.mark_progress_broadcast(transfer_id)
        percent = (
            round(100.0 * state.bytes_transferred / state.size, 1)
            if state.size > 0
            else 100.0
        )
        await self._emit(
            "transfer_progress",
            {
                "id": state.id,
                "bytes": state.bytes_transferred,
                "percent": min(percent, 100.0),
                "speed_bps": hub.speed_for(state.id),
            },
        )

    async def transfer_done(self, state: TransferState) -> None:
        hub.finalize_transfer(state)
        await self._emit("transfer_done", state.model_dump(mode="json"))
        await self.scheduler_update()

    async def transfer_failed(self, transfer_id: str, error: str) -> None:
        state = hub.get_transfer(transfer_id)
        if state is not None:
            state.status = TransferStatus.FAILED
            state.error = error
            state.finished_at = datetime.now(timezone.utc)
            hub.finalize_transfer(state)
        await self._emit("transfer_failed", {"id": transfer_id, "error": error})
        await self.scheduler_update()

    # ----------------------------------------------------------- scheduler --

    async def scheduler_update(self) -> None:
        """Personalized lane info; only pushed when something changed."""
        active_streams = hub.active_stream_count()
        budget = hub.config.global_lane_budget
        lanes = _lane_scheduler_mod.lane_scheduler.lanes_for(hub.user_count())
        signature = (budget, active_streams, lanes, hub.user_count())
        if signature == getattr(self, "_scheduler_signature", None):
            return
        self._scheduler_signature = signature
        for device in hub.approved_devices():
            await manager.send_to(
                device.id,
                {
                    "type": "scheduler_update",
                    "payload": {
                        "global_budget": budget,
                        "active_streams": active_streams,
                        "your_lanes": lanes,
                    },
                },
            )

    # -------------------------------------------------------------- relay --

    async def relay_offer(
        self, state: TransferState, from_device: DeviceInfo, sha256: str
    ) -> None:
        """Incoming offer — delivered only to the receiver."""
        await manager.send_to(
            state.receiver_id,
            {
                "type": "relay_offer",
                "payload": {
                    "transfer_id": state.id,
                    "from_device": from_device.model_dump(mode="json"),
                    "filename": state.filename,
                    "size": state.size,
                    "sha256": sha256,
                },
            },
        )
        await self.transfer_start(state)

    async def relay_accepted(
        self, transfer_id: str, receiver_name: str, sender_id: str
    ) -> None:
        """Tell the sender that the receiver is ready for chunks."""
        await manager.send_to(
            sender_id,
            {
                "type": "relay_accepted",
                "payload": {
                    "transfer_id": transfer_id,
                    "receiver_name": receiver_name,
                },
            },
        )

    async def relay_rejected(
        self, transfer_id: str, reason: str, sender_id: str
    ) -> None:
        """Tell the sender the receiver declined."""
        await manager.send_to(
            sender_id,
            {
                "type": "relay_rejected",
                "payload": {"transfer_id": transfer_id, "reason": reason},
            },
        )

    async def relay_finished(self, transfer_id: str, receiver_id: str) -> None:
        """Tell the receiver that every chunk has been forwarded."""
        await manager.send_to(
            receiver_id,
            {
                "type": "relay_finished",
                "payload": {"transfer_id": transfer_id},
            },
        )

    async def relay_aborted(
        self, transfer_id: str, reason: str, endpoints: list[str]
    ) -> None:
        await manager.broadcast(
            {
                "type": "relay_aborted",
                "payload": {"transfer_id": transfer_id, "reason": reason},
            },
            device_ids=endpoints,
        )


# Process-wide singleton.
broadcaster = Broadcaster()
