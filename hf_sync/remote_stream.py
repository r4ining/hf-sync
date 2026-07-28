"""A re-openable, file-like wrapper around a remote HTTP GET stream.

Data is read from the source in chunks and, via
:func:`hf_sync.providers.resumable.spool_to_file`, written straight into a
local ``.part`` temp file before being handed to the destination SDK. This
keeps the source read to a single pass -- providers no longer need to
re-download the file to rewind, since the SDK reads the spooled local file
(which is trivially seekable) instead of this stream directly.

:meth:`seek` (only supported for ``seek(0)``) is kept for completeness and
transparently re-issues the GET request against the source instead of
rewinding a buffer, since a live HTTP response is not itself seekable.
"""

from __future__ import annotations

import io
from typing import Callable, Optional

import requests

DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB


class RemoteReadStream(io.RawIOBase):
    """File-like object that streams bytes from a remote URL on demand.

    ``opener`` is called with a byte offset (0 for a normal open) and must
    issue the GET request, sending an HTTP ``Range: bytes=<offset>-`` header
    whenever the offset is non-zero so the source can resume a partial
    transfer instead of restarting from the beginning.
    """

    def __init__(
        self,
        opener: Callable[[int], requests.Response],
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        super().__init__()
        self._opener = opener
        self._chunk_size = chunk_size
        self._response: Optional[requests.Response] = None
        self._iterator = None
        self._buffer = b""
        self._pos = 0
        self.resume_supported = True
        self._open(0)

    # -- internal helpers -------------------------------------------------
    def _open(self, offset: int = 0) -> None:
        self._close_response()
        self._response = self._opener(offset)
        self._response.raise_for_status()
        self._buffer = b""
        if offset and self._response.status_code != 206:
            # The server ignored our Range request and sent the full body
            # back from byte 0 instead. Let the caller know so it can
            # discard any locally-cached bytes and restart from scratch.
            self.resume_supported = False
            self._pos = 0
        else:
            self.resume_supported = True
            self._pos = offset
        self._iterator = self._response.iter_content(chunk_size=self._chunk_size)

    def reopen_from(self, offset: int) -> bool:
        """Re-issue the GET request starting at ``offset`` bytes.

        Returns ``True`` if the server honored the Range request (HTTP 206)
        and reading will continue from ``offset``; returns ``False`` if the
        server ignored it and sent the full body from byte 0 instead.
        """
        self._open(offset)
        return self.resume_supported

    def _close_response(self) -> None:
        if self._response is not None:
            try:
                self._response.close()
            except Exception:
                pass
        self._response = None
        self._iterator = None

    def _fill_buffer(self) -> bool:
        """Pull the next chunk from the source. Returns False at EOF."""
        if self._iterator is None:
            return False
        try:
            chunk = next(self._iterator)
        except StopIteration:
            return False
        self._buffer += chunk
        return True

    # -- io.RawIOBase interface -------------------------------------------
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        target = offset if whence == io.SEEK_SET else None
        if whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            raise io.UnsupportedOperation("seek from end is not supported on a remote stream")
        if target is None:
            raise io.UnsupportedOperation(f"unsupported whence={whence}")
        if target == self._pos:
            return self._pos
        if target != 0:
            raise io.UnsupportedOperation(
                "RemoteReadStream only supports rewinding to the start (seek(0)); "
                f"requested seek to offset {target}"
            )
        # Rewind to the beginning: re-issue the GET request against the source.
        self._open()
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunks = [self._buffer]
            self._buffer = b""
            while self._fill_buffer():
                chunks.append(self._buffer)
                self._buffer = b""
            data = b"".join(chunks)
            self._pos += len(data)
            return data

        while len(self._buffer) < size and self._fill_buffer():
            pass
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        self._pos += len(data)
        return data

    def readinto(self, b) -> int:  # type: ignore[override]
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n

    def close(self) -> None:
        self._close_response()
        super().close()
