"""Shared folder operations: listing, safe path resolution, delete.

The shared folder is the ONLY public folder. Every name coming from a
client is untrusted and goes through ``resolve()`` first.
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import aiofiles

from app.config import ServerConfig, get_config
from app.models.schemas import FileInfo

logger = logging.getLogger(__name__)


class BadPathError(ValueError):
    """Raised when a client-supplied file name is unsafe."""


class NotFoundError(FileNotFoundError):
    """Raised when a file does not exist in the shared folder."""


class FileManager:
    """Everything that touches ``shared/`` goes through here."""

    def __init__(self, config: ServerConfig | None = None) -> None:
        self.config = config or get_config()
        self.shared_dir: Path = self.config.shared_dir.resolve()
        self.shared_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- listing --

    def list_files(self) -> list[FileInfo]:
        """List files in shared/ (hidden entries are never exposed)."""
        files: list[FileInfo] = []
        try:
            entries = sorted(self.shared_dir.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return files
        for path in entries:
            if path.name.startswith("."):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if not path.is_file():
                continue
            files.append(
                FileInfo(
                    name=path.name,
                    size=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    mime=mimetypes.guess_type(path.name)[0],
                )
            )
        return files

    def fingerprint(self) -> dict[str, tuple[int, int]]:
        """Cheap change-detection snapshot: {name: (size, mtime_ns)}."""
        result: dict[str, tuple[int, int]] = {}
        for path in self.shared_dir.iterdir():
            if path.name.startswith("."):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file():
                result[path.name] = (stat.st_size, stat.st_mtime_ns)
        return result

    # ----------------------------------------------------------- safe paths --

    @staticmethod
    def sanitize(name: str) -> str:
        """Validate a client-supplied file name; raise BadPathError if unsafe."""
        name = (name or "").strip()
        if not name:
            raise BadPathError("empty file name")
        if len(name) > 255:
            raise BadPathError("file name too long")
        if "\x00" in name:
            raise BadPathError("invalid file name")
        if "/" in name or "\\" in name:
            raise BadPathError("path separators are not allowed")
        if name in (".", "..") or name.startswith("."):
            raise BadPathError("hidden or relative names are not allowed")
        if ":" in name[:2] or (len(name) > 1 and name[1] == ":"):
            raise BadPathError("drive letters are not allowed")
        return name

    def resolve(self, name: str) -> Path:
        """Resolve a name inside shared/ or raise (BadPath/NotFound)."""
        safe = self.sanitize(name)
        path = (self.shared_dir / safe).resolve()
        if path.parent != self.shared_dir:
            raise BadPathError("path escapes the shared folder")
        if not path.is_file():
            raise NotFoundError(f"no such file: {safe}")
        return path

    def unique_name(self, name: str) -> str:
        """Return ``name`` or ``name (1)``, ``name (2)``… so it never overwrites."""
        safe = self.sanitize(name)
        candidate = self.shared_dir / safe
        if not candidate.exists():
            return safe
        stem, suffix = Path(safe).stem, Path(safe).suffix
        counter = 1
        while True:
            alternative = f"{stem} ({counter}){suffix}"
            if not (self.shared_dir / alternative).exists():
                return alternative
            counter += 1

    # --------------------------------------------------------------- write --

    async def save_stream(self, name: str, stream: AsyncIterator[bytes]) -> Path:
        """Write an async byte stream into shared/ under a safe unique name."""
        final_name = self.unique_name(self.sanitize(name))
        target = self.shared_dir / final_name
        async with aiofiles.open(target, "wb") as out:
            async for chunk in stream:
                if chunk:
                    await out.write(chunk)
        logger.info("Saved stream to %s (%d bytes)", final_name, target.stat().st_size)
        return target

    def delete(self, name: str) -> None:
        """Delete a file from shared/ (host-only enforcement happens at the API)."""
        path = self.resolve(name)
        path.unlink()
        logger.info("Deleted %s", path.name)

    # --------------------------------------------------------------- moves --

    def move_into_shared(self, source: Path, name: str) -> Path:
        """Move ``source`` into shared/ under a unique safe name."""
        final_name = self.unique_name(self.sanitize(name))
        target = self.shared_dir / final_name
        source.replace(target)
        return target


# Process-wide singleton.
file_manager = FileManager()
