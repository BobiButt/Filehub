"""Upload API: init / chunk / complete / status / resume (§8, §11.1).

The server never holds the whole file in RAM — chunks are read once,
CRC-checked, and written straight into the pre-allocated part file.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from app.api import ApiError, require_member
from app.config import get_config
from app.core.broadcaster import broadcaster
from app.core.file_manager import BadPathError, file_manager
from app.core.hub import hub
from app.core.upload_session import (
    Sha256Mismatch,
    UploadError,
    bitmap_to_hex,
    upload_sessions,
)
from app.models.schemas import (
    TransferKind,
    TransferState,
    TransferStatus,
    UploadCompleteRequest,
    UploadInitRequest,
    UploadResumeRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()
config = get_config()


def _valid_sha(value: str) -> bool:
    """True if the value is a valid 64-char hex SHA-256, OR empty
    (meaning "client skipped hashing — trust the server's computation")."""
    value = (value or "").lower()
    if value == "":
        return True
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _normalize_sha(value: str | None) -> str:
    """Lowercase and strip; empty string means 'no client hash provided'."""
    return (value or "").strip().lower()


async def _ensure_transfer(session, device_id: str) -> TransferState:
    """Mirror the session into a TransferState so dashboards can track it."""
    state = hub.get_transfer(session.session_id)
    if state is not None and state.status not in (
        TransferStatus.DONE,
        TransferStatus.FAILED,
        TransferStatus.REJECTED,
        TransferStatus.CANCELLED,
    ):
        return state
    state = TransferState(
        id=session.session_id,
        kind=TransferKind.UPLOAD,
        sender_id=device_id,
        receiver_id="server",
        filename=session.filename,
        size=session.size,
        bytes_transferred=session.bytes_received,
        chunks_total=session.chunks_total,
        chunks_received=session.received,
        status=TransferStatus.ACTIVE,
        started_at=datetime.now(timezone.utc),
    )
    hub.create_transfer(state)
    await broadcaster.transfer_start(state)
    actor = hub.get_device(device_id)
    await broadcaster.log(
        TransferKind.UPLOAD,
        actor.name if actor else "?",
        "SERVER",
        "started",
        filename=session.filename,
        size=session.size,
        transfer_id=session.session_id,
    )
    return state


@router.post("/api/upload/init")
async def upload_init(body: UploadInitRequest, request: Request) -> dict:
    """Start (or resume) a chunked upload session."""
    device = require_member(request)
    if body.size < 0 or body.size > config.max_file_size:
        raise ApiError(413, "file too large")
    sha = _normalize_sha(body.sha256)
    if not _valid_sha(sha):
        raise ApiError(400, "invalid sha256")
    try:
        filename = file_manager.sanitize(body.filename)
    except BadPathError as exc:
        raise ApiError(400, str(exc)) from exc

    # The server's chunk size is authoritative; the client adapts to it.
    chunk_size = config.chunk_size
    session = upload_sessions.find(filename, body.size, sha)
    if session is None:
        session = upload_sessions.create(
            filename=filename,
            size=body.size,
            sha256=sha,
            chunk_size=chunk_size,
        )
    state = await _ensure_transfer(session, device.id)
    logger.info("Upload init %s for %r by %r", session.session_id, filename, device.name)
    return {
        "session_id": session.session_id,
        "existing_bitmap": bitmap_to_hex(session.bitmap) if session.received else None,
        "chunk_size": session.chunk_size,
        "transfer_id": state.id,
    }


@router.post("/api/upload/chunk")
async def upload_chunk(
    request: Request,
    session_id: str = Form(...),
    index: int = Form(...),
    crc32: int = Form(...),
    payload: UploadFile = File(...),
) -> dict:
    """Receive one 1 MB chunk: verify CRC32, write at its offset (§11.4)."""
    require_member(request)
    session = upload_sessions.get(session_id)
    data = await payload.read(session.chunk_size + 1)
    if len(data) > session.chunk_size:
        raise ApiError(400, "chunk larger than chunk_size")
    try:
        is_new = await session.write_chunk(index, data, crc32)
    except UploadError as exc:
        raise ApiError(exc.status, exc.message) from exc

    state = hub.get_transfer(session_id)
    if state is not None and state.status == TransferStatus.ACTIVE:
        state.bytes_transferred = session.bytes_received
        state.chunks_received = session.received
        hub.touch_transfer(session_id)
        hub.record_progress(session_id, state.bytes_transferred)
        await broadcaster.transfer_progress(
            session_id, force=is_new and session.is_complete()
        )
    return {"received": session.received, "total": session.chunks_total}


@router.post("/api/upload/complete")
async def upload_complete(body: UploadCompleteRequest, request: Request):
    """Verify whole-file SHA-256, move tmp → shared/, notify dashboards."""
    device = require_member(request)
    session = upload_sessions.get(body.session_id)
    if not session.is_complete():
        missing = session.missing_indices()
        return JSONResponse(
            status_code=400,
            content={
                "error": f"{len(missing)} chunks missing",
                "missing": missing[:500],
            },
        )
    try:
        target = await session.finalize()
    except Sha256Mismatch as exc:
        logger.warning("SHA-256 mismatch for %s", session.session_id)
        return JSONResponse(
            status_code=422,
            content={
                "error": "sha256 mismatch",
                "server_sha256": exc.server_sha256,
                "expected_sha256": session.sha256,
            },
        )
    except UploadError as exc:
        raise ApiError(exc.status, exc.message) from exc

    state = hub.get_transfer(session.session_id)
    transfer_id = session.session_id
    final_name = target.name
    size = session.size
    upload_sessions.remove(session.session_id)
    if state is not None:
        state.status = TransferStatus.DONE
        state.bytes_transferred = size
        state.chunks_received = state.chunks_total
        state.filename = final_name
        state.finished_at = datetime.now(timezone.utc)
        await broadcaster.transfer_done(state)
    files = file_manager.list_files()
    await broadcaster.files_changed(files)
    await broadcaster.log(
        TransferKind.UPLOAD,
        device.name,
        "SERVER",
        "done",
        filename=final_name,
        size=size,
        transfer_id=transfer_id,
    )
    return {"ok": True, "name": final_name}


@router.get("/api/upload/status/{session_id}")
async def upload_status(session_id: str, request: Request) -> dict:
    """Current bitmap for a session (used to rebuild state after a drop)."""
    require_member(request)
    session = upload_sessions.get(session_id)
    return {
        "session_id": session.session_id,
        "bitmap": bitmap_to_hex(session.bitmap),
        "received": session.received,
        "total": session.chunks_total,
        "missing": session.missing_indices()[:500],
        "chunk_size": session.chunk_size,
    }


@router.post("/api/upload/resume")
async def upload_resume(body: UploadResumeRequest, request: Request):
    """Return the chunks that still need sending (§11.1 Resume).

    With ``crcs`` (after a SHA mismatch) the server compares the client's
    per-chunk CRC32s against what it actually has on disk and returns
    exactly the chunks that differ (§13).
    """
    require_member(request)
    sha = _normalize_sha(body.sha256)
    if not _valid_sha(sha):
        raise ApiError(400, "invalid sha256")
    try:
        filename = file_manager.sanitize(body.filename)
    except BadPathError as exc:
        raise ApiError(400, str(exc)) from exc
    session = upload_sessions.find(filename, body.size, sha)
    if session is None:
        chunk_size = config.chunk_size
        total = (body.size + chunk_size - 1) // chunk_size
        return {
            "session_id": None,
            "missing": list(range(total)),
            "bitmap": None,
            "chunk_size": chunk_size,
        }
    missing = await session.indices_to_resend(body.crcs)
    return {
        "session_id": session.session_id,
        "missing": missing,
        "bitmap": bitmap_to_hex(session.bitmap),
        "chunk_size": session.chunk_size,
    }