"""Files API: list shared/ and host-only delete (§8 Files)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api import ApiError, require_host, require_member
from app.core.broadcaster import broadcaster
from app.core.file_manager import BadPathError, NotFoundError, file_manager

router = APIRouter()


@router.get("/api/files")
async def list_files(request: Request) -> dict:
    """Everything currently in the shared folder."""
    require_member(request)
    return {"files": [f.model_dump(mode="json") for f in file_manager.list_files()]}


@router.delete("/api/files/{name}")
async def delete_file(name: str, request: Request) -> dict:
    """Host-only delete; everyone's Panel 1 updates instantly."""
    actor = require_host(request)
    try:
        file_manager.delete(name)
    except BadPathError as exc:
        raise ApiError(400, str(exc)) from exc
    except NotFoundError as exc:
        raise ApiError(404, str(exc)) from exc
    files = file_manager.list_files()
    await broadcaster.files_changed(files)
    await broadcaster.log("admin", actor, "SERVER", "deleted", filename=name)
    return {"ok": True}
