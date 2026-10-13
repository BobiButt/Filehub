"""Pydantic models: domain objects and the WebSocket envelope."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel


class DeviceState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    BLOCKED = "blocked"


class DeviceInfo(BaseModel):
    id: str  # server-generated UUID
    name: str  # user-chosen display name
    os: str  # "Android" | "iOS" | "Windows" | "macOS" | "Linux" | "Unknown"
    user_agent: str
    fingerprint: str  # hash of UA + screen + language + tz
    ip: str  # RAM only, never persisted
    state: DeviceState
    role: Literal["host", "guest"]
    connected_at: datetime
    last_seen: datetime


class FileInfo(BaseModel):
    name: str
    size: int
    modified: datetime
    mime: str | None = None


class TransferStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class TransferKind(str, Enum):
    UPLOAD = "upload"  # client -> server
    DOWNLOAD = "download"  # server -> client
    RELAY = "relay"  # client -> client


class TransferState(BaseModel):
    id: str
    kind: TransferKind
    sender_id: str  # "server" for downloads
    receiver_id: str  # "server" for uploads
    filename: str
    size: int
    bytes_transferred: int = 0
    chunks_total: int
    chunks_received: int = 0
    status: TransferStatus
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None


class TrafficEvent(BaseModel):
    ts: datetime
    kind: TransferKind | Literal["auth", "admin"]
    actor: str  # device name
    target: str  # "SERVER" or device name
    filename: str | None = None
    size: int | None = None
    status: str
    transfer_id: str | None = None


class WSMessage(BaseModel):
    type: str
    payload: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# HTTP request bodies
# ---------------------------------------------------------------------------


class JoinRequest(BaseModel):
    code: str = ""
    name: str
    screen: str = ""  # e.g. "1920x1080", part of the fingerprint
    tz: str = ""  # e.g. "Asia/Riyadh", part of the fingerprint


class ApproveRequest(BaseModel):
    device_id: str
    action: Literal["approve", "reject"]


class DeviceRequest(BaseModel):
    device_id: str


class UploadInitRequest(BaseModel):
    filename: str
    size: int
    sha256: str
    chunk_size: int


class UploadCompleteRequest(BaseModel):
    session_id: str


class UploadResumeRequest(BaseModel):
    filename: str
    size: int
    sha256: str
    crcs: list[int] | None = None  # per-chunk CRC32s after a SHA mismatch
