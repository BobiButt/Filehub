"""FastAPI application: mounts routers/static/templates and the §9 glue."""

from __future__ import annotations

import asyncio
import io
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import qrcode
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.api import ApiError
from app.api.auth import router as auth_router
from app.api.download import router as download_router
from app.api.files import router as files_router
from app.api.upload import router as upload_router
from app.api.ws import router as ws_router
from app.config import get_config, get_lan_ip
from app.core.broadcaster import broadcaster
from app.core.connection_manager import manager
from app.core.file_manager import file_manager
from app.core.hub import hub
from app.core.relay import relay_service
from app.core.security import ADMIN_COOKIE, SESSION_COOKIE, security
from app.core.upload_session import upload_sessions

logger = logging.getLogger(__name__)
config = get_config()

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

IDLE_TRANSFER_TIMEOUT = 300.0  # abandoned uploads/offers must not hold lanes


def _server_url() -> str:
    lan_ip = get_lan_ip()
    if lan_ip.startswith("127."):
        return f"http://127.0.0.1:{config.port}"
    return f"http://{lan_ip}:{config.port}"


async def _drop_device(device_id: str, reason: str) -> None:
    """Full presence removal: relays → socket → broadcast (§9.1/§9.5)."""
    await relay_service.abort_for_device(device_id, reason)
    await manager.disconnect(device_id)
    device = hub.unregister_device(device_id)
    if device is None:
        return
    await broadcaster.device_left(device_id)
    await broadcaster.log("auth", device.name, "SERVER", "left")
    await broadcaster.scheduler_update()


async def _heartbeat_loop() -> None:
    """Ping every 20 s; drop silent sockets after 60 s; sweep idle transfers."""
    while True:
        await asyncio.sleep(config.heartbeat_interval_seconds)
        try:
            await manager.send_pings()
            now = time.monotonic()
            for device in list(hub.devices.values()):
                runtime = hub.runtime(device.id)
                stale = False
                if manager.is_connected(device.id):
                    stale = (
                        manager.seconds_since_message(device.id)
                        > config.heartbeat_timeout_seconds
                    )
                elif (
                    runtime.offline_at is not None
                    and now - runtime.offline_at > config.heartbeat_timeout_seconds
                ):
                    stale = True
                if stale:
                    logger.info("Dropping %r: heartbeat timeout", device.name)
                    await _drop_device(device.id, "heartbeat timeout")
            hub.prune_finalized()
            for state in hub.active_transfers():
                idle = hub.transfer_idle(state.id)
                if idle > IDLE_TRANSFER_TIMEOUT:
                    logger.info("Transfer %s idle for %.0fs, failing", state.id, idle)
                    if state.id in relay_service.relays:
                        await relay_service.abort(state.id, "timed out")
                    else:
                        await broadcaster.transfer_failed(state.id, "timed out")
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("heartbeat loop error")


async def _shared_folder_loop() -> None:
    """Watch shared/ for external changes (e.g. files copied in by hand)."""
    last = file_manager.fingerprint()
    while True:
        await asyncio.sleep(2.0)
        try:
            current = file_manager.fingerprint()
        except OSError:
            continue
        if current != last:
            last = current
            logger.info("shared/ changed on disk, broadcasting files_changed")
            await broadcaster.files_changed(file_manager.list_files())


@asynccontextmanager
async def lifespan(_: FastAPI):
    (config.shared_dir).mkdir(parents=True, exist_ok=True)
    (config.tmp_dir).mkdir(parents=True, exist_ok=True)
    (config.sessions_dir).mkdir(parents=True, exist_ok=True)
    restored = upload_sessions.load_all()
    logger.info("FileHub ready — %d resumable upload session(s)", restored)
    tasks = [
        asyncio.create_task(_heartbeat_loop()),
        asyncio.create_task(_shared_folder_loop()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for device_id in list(manager.sockets):
            await manager.disconnect(device_id)
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="FileHub", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ------------------------------------------------------------- errors ----


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": "internal server error"})


# -------------------------------------------------------------- routes ----


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    """Dashboard for everyone; ?admin=TOKEN seeds the HttpOnly admin cookie."""
    admin_token = request.query_params.get("admin", "")
    if admin_token and secrets.compare_digest(admin_token, hub.admin_token):
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(
            ADMIN_COOKIE, hub.admin_token, httponly=True, samesite="lax"
        )
        return response

    device_id = request.cookies.get(SESSION_COOKIE, "")
    device = hub.get_device(device_id) if device_id else None
    is_host = security.is_host(request, hub.admin_token)
    show_code = is_host or (device is not None and device.role == "host")
    context = {
        "request": request,
        "has_session": device is not None,
        "device": (
            {
                "id": device.id,
                "name": device.name,
                "role": device.role,
                "state": device.state.value,
            }
            if device is not None
            else None
        ),
        "join_code": hub.join_code if show_code else None,
        "is_host": is_host,
        "server_url": _server_url(),
        "chunk_size": config.chunk_size,
        "max_file_size": config.max_file_size,
    }
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/api/qr")
async def qr_code() -> Response:
    """PNG QR code of the server URL for the dashboard QR modal (§10 top bar)."""
    url = _server_url() + "/"
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    pil_image = image.get_image() if hasattr(image, "get_image") else image
    buffer = io.BytesIO()
    pil_image.save(buffer, format="PNG")
    return Response(content=buffer.getvalue(), media_type="image/png")


app.include_router(auth_router)
app.include_router(files_router)
app.include_router(download_router)
app.include_router(upload_router)
app.include_router(ws_router)
