"""Download API: GET /api/download/{name} with HTTP Range (§8, §11.2).

Starlette's FileResponse handles Range/resume natively; the transfer is
mirrored into the WS dashboard so Panel 3 shows it live.
"""

from __future__ import annotations

import mimetypes
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.api import ApiError, require_member
from app.core.broadcaster import broadcaster
from app.core.file_manager import BadPathError, NotFoundError, file_manager
from app.core.hub import hub
from app.models.schemas import TransferKind, TransferState, TransferStatus

router = APIRouter()


@router.get("/api/download/{name}")
async def download_file(name: str, request: Request) -> FileResponse:
    device = require_member(request)
    try:
        path = file_manager.resolve(name)
    except BadPathError as exc:
        raise ApiError(400, str(exc)) from exc
    except NotFoundError as exc:
        raise ApiError(404, str(exc)) from exc

    size = path.stat().st_size
    transfer_id = str(uuid.uuid4())
    state = TransferState(
        id=transfer_id,
        kind=TransferKind.DOWNLOAD,
        sender_id="server",
        receiver_id=device.id,
        filename=path.name,
        size=size,
        chunks_total=(size + hub.config.chunk_size - 1) // hub.config.chunk_size,
        status=TransferStatus.ACTIVE,
        started_at=datetime.now(timezone.utc),
    )
    hub.create_transfer(state)
    await broadcaster.transfer_start(state)
    await broadcaster.log(
        TransferKind.DOWNLOAD,
        device.name,
        "SERVER",
        "started",
        filename=path.name,
        size=size,
        transfer_id=transfer_id,
    )

    async def _finish() -> None:
        """Runs after the response is fully streamed (§7.1 transfer_done)."""
        current = hub.get_transfer(transfer_id)
        if current is None or current.status != TransferStatus.ACTIVE:
            return
        current.status = TransferStatus.DONE
        current.bytes_transferred = current.size
        current.chunks_received = current.chunks_total
        current.finished_at = datetime.now(timezone.utc)
        await broadcaster.transfer_done(current)
        await broadcaster.log(
            TransferKind.DOWNLOAD,
            device.name,
            "SERVER",
            "done",
            filename=current.filename,
            size=current.size,
            transfer_id=transfer_id,
        )

    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        headers={"X-Transfer-Id": transfer_id},
        background=BackgroundTask(_finish),
    )
