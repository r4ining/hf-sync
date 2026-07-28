"""Shared helper for resuming interrupted transfers spooled to local disk.

Any provider whose ``upload()`` (or, for :class:`~hf_sync.providers.local_provider.LocalProvider`,
its final destination write) writes a stream to a local ``.part`` file
before finishing the transfer can use :func:`spool_to_file` here to survive
an interrupted run: instead of starting over from byte 0 on the next sync,
the stable ``.part`` file left on disk lets us pick up where we left off via
an HTTP Range request against the source (when the source stream is backed
by a :class:`~hf_sync.remote_stream.RemoteReadStream`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from typing import IO, Optional

from hf_sync.progress import ProgressStream
from hf_sync.remote_stream import RemoteReadStream

logger = logging.getLogger("hf_sync")

# Every file is spooled through a local temp file before being handed to the
# destination SDK. This avoids feeding the SDK a live, non-rewindable HTTP
# stream directly -- both the Hugging Face and ModelScope upload paths read
# the file content twice (once to hash it, once to transmit it), and a
# non-seekable remote stream would otherwise have to be re-downloaded from
# the source for the second read. Spooling to disk also lets the SDK take
# its disk-based, chunked-hashing path instead of buffering the whole file
# in memory, and, as a side effect, is what makes resuming an interrupted
# transfer possible.
#
# Files are spooled into this directory instead of a random tempfile so
# that a partially-downloaded ``.part`` file survives an interrupted sync
# run (Ctrl-C, network failure, process crash, etc.) and can be resumed --
# via an HTTP Range request against the source -- the next time the same
# file is synced, instead of re-downloading it from scratch.
PARTIAL_CACHE_DIR = os.path.join(tempfile.gettempdir(), "hf-sync-partial")


def partial_cache_info() -> tuple[str, int, int]:
    """Return ``(path, total_size_bytes, file_count)`` for the partial-download cache dir.

    If the directory does not exist or is empty, returns ``(path, 0, 0)``.
    """
    total_size = 0
    file_count = 0
    if os.path.isdir(PARTIAL_CACHE_DIR):
        for name in os.listdir(PARTIAL_CACHE_DIR):
            fp = os.path.join(PARTIAL_CACHE_DIR, name)
            if os.path.isfile(fp):
                total_size += os.path.getsize(fp)
                file_count += 1
    return PARTIAL_CACHE_DIR, total_size, file_count


def partial_file_path(key: str, path_in_repo: str) -> str:
    """Build a stable ``.part`` file path in :data:`PARTIAL_CACHE_DIR` for ``key``.

    ``key`` should uniquely identify the transfer (provider, repo, revision,
    path, size, ...); ``path_in_repo`` is only used to keep the on-disk
    filename human-readable.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    safe_name = path_in_repo.replace("/", "---")
    os.makedirs(PARTIAL_CACHE_DIR, exist_ok=True)
    return os.path.join(PARTIAL_CACHE_DIR, f"{digest}---{safe_name}.part")


def unwrap_remote_stream(stream: IO[bytes]) -> Optional[RemoteReadStream]:
    """Reach through BufferedReader(ProgressStream(RemoteReadStream(...))) wrapping."""
    raw = getattr(stream, "raw", stream)
    inner = getattr(raw, "_inner", None)
    return inner if isinstance(inner, RemoteReadStream) else None


def unwrap_progress_stream(stream: IO[bytes]) -> Optional[ProgressStream]:
    raw = getattr(stream, "raw", stream)
    return raw if isinstance(raw, ProgressStream) else None


def spool_to_file(stream: IO[bytes], tmp_path: str, size: int, *, label: str) -> bool:
    """Write ``stream`` into ``tmp_path``, resuming from a partial ``.part``
    file left behind by an earlier interrupted run when possible.

    If the source stream is backed by a :class:`RemoteReadStream` and the
    source honors HTTP Range requests, an existing partial ``tmp_path`` is
    resumed from its current size instead of being discarded. Otherwise (no
    remote stream, or the source doesn't support Range requests), any stale
    partial file is discarded and the transfer restarts from scratch.

    Returns ``True`` if ``tmp_path`` was already fully downloaded on disk
    (``stream`` is closed and nothing is copied); returns ``False`` if bytes
    were copied from ``stream`` into ``tmp_path`` (partially resumed or from
    scratch), in which case the caller is responsible for closing ``stream``.
    """
    remote = unwrap_remote_stream(stream)
    resume_offset = 0
    if os.path.exists(tmp_path):
        existing_size = os.path.getsize(tmp_path)
        if existing_size == size:
            # Already fully downloaded locally (likely crashed/interrupted
            # right before or during the upload step) -- skip straight to
            # uploading it, no need to touch the source again.
            logger.info(
                "Found fully-downloaded partial file for %s, skipping re-download ...", label,
            )
            stream.close()
            return True
        if 0 < existing_size < size and remote is not None and remote.reopen_from(existing_size):
            resume_offset = existing_size
            progress = unwrap_progress_stream(stream)
            if progress is not None:
                progress.set_progress(existing_size)
            logger.info(
                "Resuming interrupted download of %s from %s/%s bytes ...",
                label, f"{existing_size:,}", f"{size:,}",
            )
        else:
            # Stale, corrupt, or the source doesn't support Range
            # requests -- discard and start over from scratch.
            os.remove(tmp_path)

    logger.info(
        "Spooling %s (%s bytes) to local temp file: %s%s",
        label, f"{size:,}", tmp_path,
        f", resuming from {resume_offset:,} bytes" if resume_offset else "",
    )
    mode = "ab" if resume_offset else "wb"
    with open(tmp_path, mode) as tmp:
        shutil.copyfileobj(stream, tmp, length=16 * 1024 * 1024)
    return False
