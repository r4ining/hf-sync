"""A re-openable, file-like wrapper around a remote HTTP GET stream.

This is the core piece that lets us bridge two remote repositories without
ever writing a complete file to local disk:

* Data is read from the source in chunks and forwarded straight into the
  destination SDK's upload call (``path_or_fileobj=stream``).
* Both the Hugging Face and ModelScope upload code paths need to read the
  file content once to compute its hash and once more to actually transmit
  it. A live HTTP response is not seekable, so :meth:`seek` (only supported
  for ``seek(0)``) transparently re-issues the GET request against the
  source instead of rewinding a buffer. No bytes are ever spooled to disk;
  at most a small in-flight chunk lives in memory.

The trade-off (explicitly agreed on with the user): whenever a rewind is
needed, the source file is re-downloaded from the network. Bandwidth cost
can double, but local disk usage stays at zero regardless of file size.
"""

from __future__ import annotations

import io
from typing import Callable, Optional

import requests

DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB


class RemoteReadStream(io.RawIOBase):
    """File-like object that streams bytes from a remote URL on demand."""

    def __init__(
        self,
        opener: Callable[[], requests.Response],
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
        self._open()

    # -- internal helpers -------------------------------------------------
    def _open(self) -> None:
        self._close_response()
        self._response = self._opener()
        self._response.raise_for_status()
        self._iterator = self._response.iter_content(chunk_size=self._chunk_size)
        self._buffer = b""
        self._pos = 0

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
