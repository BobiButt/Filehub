"""Chunked, resumable upload sessions (§9.4).

Each session is a JSON manifest at ``sessions/{session_id}.json`` plus a
pre-allocated part file at ``tmp/{session_id}.part``. Chunks are written
at ``index * chunk_size`` (random access) and tracked with a 1-bit-per-
chunk bitmap, hex-encoded in the manifest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path

import aiofiles

from app.config import ServerConfig, get_config
from app.core.file_manager import file_manager

logger = logging.getLogger(__name__)

_SHA_BLOCK = 4 * 1024 * 1024


class UploadError(Exception):
    """Recoverable upload error (rendered as JSON with ``status``)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class Sha256Mismatch(UploadError):
    """Whole-file SHA-256 did not match after assembly (§13)."""

    def __init__(self, server_sha256: str) -> None:
        super().__init__("sha256 mismatch", status=422)
        self.server_sha256 = server_sha256


# --------------------------------------------------------------- bitmap ----


def make_bitmap(chunks: int) -> bytearray:
    return bytearray((chunks + 7) // 8)


def bitmap_to_hex(bitmap: bytearray) -> str:
    return bytes(bitmap).hex()


def bitmap_from_hex(value: str, chunks: int) -> bytearray:
    try:
        data = bytearray(bytes.fromhex(value or ""))
    except ValueError:
        data = bytearray()
    expected = (chunks + 7) // 8
    if len(data) < expected:
        data.extend(b"\x00" * (expected - len(data)))
    return data


def _sha256_file(path: Path) -> str:
    """Blocking whole-file SHA-256 (run in a thread)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(_SHA_BLOCK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class UploadSession:
    """One resumable upload: manifest + pre-allocated part file."""

    def __init__(
        self,
        *,
        session_id: str,
        filename: str,
        size: int,
        chunk_size: int,
        sha256: str,
        bitmap: bytearray,
        created_at: str,
        last_chunk_at: str,
        tmp_path: Path,
        manifest_path: Path,
    ) -> None:
        self.session_id = session_id
        self.filename = filename
        self.size = size
        self.chunk_size = chunk_size
        self.sha256 = sha256.lower()
        self.bitmap = bitmap
        self.created_at = created_at
        self.last_chunk_at = last_chunk_at
        self.tmp_path = tmp_path
        self.manifest_path = manifest_path
        self.received = self._count_bits()
        self._last_manifest_save = 0.0

    # ------------------------------------------------------------ helpers --

    @property
    def chunks_total(self) -> int:
        return (self.size + self.chunk_size - 1) // self.chunk_size

    def _count_bits(self) -> int:
        return sum(bin(b).count("1") for b in self.bitmap)

    def has(self, index: int) -> bool:
        byte, bit = divmod(index, 8)
        if byte >= len(self.bitmap):
            return False
        return bool(self.bitmap[byte] & (1 << (7 - bit)))

    def _set(self, index: int) -> bool:
        """Set a bit; returns True if it was not set before."""
        byte, bit = divmod(index, 8)
        mask = 1 << (7 - bit)
        already = bool(self.bitmap[byte] & mask)
        if not already:
            self.bitmap[byte] |= mask
            self.received += 1
        return not already

    def missing_indices(self) -> list[int]:
        return [i for i in range(self.chunks_total) if not self.has(i)]

    def is_complete(self) -> bool:
        return self.received >= self.chunks_total

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "filename": self.filename,
            "size": self.size,
            "chunk_size": self.chunk_size,
            "sha256": self.sha256,
            "bitmap_hex": bitmap_to_hex(self.bitmap),
            "created_at": self.created_at,
            "last_chunk_at": self.last_chunk_at,
        }

    async def save_manifest(self, force: bool = False) -> None:
        """Persist the manifest; throttled to once per second (§14)."""
        now = time.monotonic()
        if not force and now - self._last_manifest_save < 1.0:
            return
        self._last_manifest_save = now
        payload = json.dumps(self.to_dict(), indent=2)
        async with aiofiles.open(self.manifest_path, "w") as fh:
            await fh.write(payload)

    # ------------------------------------------------------------- chunks --

    async def write_chunk(self, index: int, data: bytes, crc32_value: int) -> bool:
        """Verify and write one chunk at its byte offset.

        Returns True if this chunk had not been received before.
        Raises UploadError on bad index/CRC/length.
        """
        if index < 0 or index >= self.chunks_total:
            raise UploadError("chunk index out of range")
        if len(data) > self.chunk_size:
            raise UploadError("chunk larger than chunk_size")
        offset = index * self.chunk_size
        expected_len = min(self.chunk_size, self.size - offset)
        if len(data) != expected_len:
            raise UploadError("chunk length mismatch")
        actual_crc = zlib.crc32(data) & 0xFFFFFFFF
        if actual_crc != (crc32_value & 0xFFFFFFFF):
            raise UploadError("crc32 mismatch")
        async with aiofiles.open(self.tmp_path, "r+b") as fh:
            await fh.seek(offset)
            await fh.write(data)
        is_new = self._set(index)
        self.last_chunk_at = datetime.now(timezone.utc).isoformat()
        await self.save_manifest(force=self.is_complete())
        logger.debug(
            "upload %s chunk %d/%d (%d bytes)",
            self.session_id,
            index,
            self.chunks_total,
            len(data),
        )
        return is_new

    @property
    def bytes_received(self) -> int:
        """Exact bytes on disk (sum of received chunk lengths)."""
        total = 0
        chunk = self.chunk_size
        for index in range(self.chunks_total):
            if self.has(index):
                total += min(chunk, self.size - index * chunk)
        return total

    # ------------------------------------------------------------- finish --

    async def finalize(self) -> Path:
        """Verify SHA-256 and move tmp → shared/. Returns the final path."""
        if not self.is_complete():
            raise UploadError(
                f"upload incomplete: {len(self.missing_indices())} chunks missing"
            )
        digest = await asyncio.to_thread(_sha256_file, self.tmp_path)
        if digest != self.sha256:
            raise Sha256Mismatch(digest)
        target = file_manager.move_into_shared(self.tmp_path, self.filename)
        logger.info("Upload %s finalized as %s", self.session_id, target.name)
        return target

    async def chunk_crcs(self) -> list[int]:
        """CRC32 of every chunk currently on disk (for SHA-mismatch repair)."""
        crcs: list[int] = []
        async with aiofiles.open(self.tmp_path, "rb") as fh:
            for index in range(self.chunks_total):
                await fh.seek(index * self.chunk_size)
                block = await fh.read(self.chunk_size)
                crcs.append(zlib.crc32(block) & 0xFFFFFFFF)
        return crcs

    async def indices_to_resend(self, client_crcs: list[int] | None) -> list[int]:
        """Missing chunks, or (with client CRCs) mismatching + missing ones."""
        missing = self.missing_indices()
        if client_crcs is None:
            return missing
        server_crcs = await self.chunk_crcs()
        bad = set(missing)
        for i, server_crc in enumerate(server_crcs):
            client_crc = (
                client_crcs[i] & 0xFFFFFFFF if i < len(client_crcs) else None
            )
            if client_crc != server_crc:
                bad.add(i)
        return sorted(bad)


class SessionStore:
    """In-memory registry of sessions, backed by sessions/*.json manifests."""

    def __init__(self, config: ServerConfig | None = None) -> None:
        self.config = config or get_config()
        self.sessions: dict[str, UploadSession] = {}
        self.config.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.config.tmp_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- paths --

    def manifest_path(self, session_id: str) -> Path:
        return self.config.sessions_dir / f"{session_id}.json"

    def tmp_path(self, session_id: str) -> Path:
        return self.config.tmp_dir / f"{session_id}.part"

    # ------------------------------------------------------------ create --

    def create(
        self, *, filename: str, size: int, sha256: str, chunk_size: int
    ) -> UploadSession:
        session_id = str(uuid.uuid4())
        chunks = (size + chunk_size - 1) // chunk_size
        tmp_path = self.tmp_path(session_id)
        with tmp_path.open("wb") as fh:
            fh.truncate(size)  # pre-allocate exact size (§11.4)
        now = datetime.now(timezone.utc).isoformat()
        session = UploadSession(
            session_id=session_id,
            filename=filename,
            size=size,
            chunk_size=chunk_size,
            sha256=sha256,
            bitmap=make_bitmap(chunks),
            created_at=now,
            last_chunk_at=now,
            tmp_path=tmp_path,
            manifest_path=self.manifest_path(session_id),
        )
        self.sessions[session_id] = session
        asyncio.get_running_loop().create_task(session.save_manifest(force=True))
        logger.info("Created upload session %s for %r (%d bytes)", session_id, filename, size)
        return session

    def find(self, filename: str, size: int, sha256: str) -> UploadSession | None:
        """Locate an existing session for the resume flow (§11.1)."""
        wanted = sha256.lower()
        for session in self.sessions.values():
            if (
                session.filename == filename
                and session.size == size
                and session.sha256 == wanted
                and session.tmp_path.exists()
            ):
                return session
        return None

    def get(self, session_id: str) -> UploadSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise UploadError("unknown upload session", status=404)
        return session

    def remove(self, session_id: str) -> None:
        """Drop a finished session and its manifest."""
        self.sessions.pop(session_id, None)
        manifest = self.manifest_path(session_id)
        try:
            manifest.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove manifest %s", manifest)

    # ------------------------------------------------------------ reload --

    def load_all(self) -> int:
        """Scan sessions/ on startup and restore resumable sessions (§14)."""
        restored = 0
        for manifest in sorted(self.config.sessions_dir.glob("*.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                chunk_size = int(data["chunk_size"])
                size = int(data["size"])
                session_id = str(data["session_id"])
                tmp_path = self.tmp_path(session_id)
                if not tmp_path.exists() or tmp_path.stat().st_size != size:
                    manifest.unlink(missing_ok=True)
                    continue
                chunks = (size + chunk_size - 1) // chunk_size
                session = UploadSession(
                    session_id=session_id,
                    filename=str(data["filename"]),
                    size=size,
                    chunk_size=chunk_size,
                    sha256=str(data["sha256"]),
                    bitmap=bitmap_from_hex(str(data.get("bitmap_hex", "")), chunks),
                    created_at=str(data.get("created_at", "")),
                    last_chunk_at=str(data.get("last_chunk_at", "")),
                    tmp_path=tmp_path,
                    manifest_path=manifest,
                )
                self.sessions[session_id] = session
                restored += 1
            except (KeyError, ValueError, TypeError, OSError) as exc:
                logger.warning("Skipping bad manifest %s: %s", manifest.name, exc)
                try:
                    manifest.unlink(missing_ok=True)
                except OSError:
                    pass
        if restored:
            logger.info("Restored %d upload session(s) for resume", restored)
        return restored


# Process-wide singleton.
upload_sessions = SessionStore()
