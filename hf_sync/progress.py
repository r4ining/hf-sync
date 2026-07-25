"""A thin progress-reporting wrapper around a file-like read stream.

Both the Hugging Face and ModelScope upload SDKs read the file-like object
we hand them (a :class:`~hf_sync.remote_stream.RemoteReadStream`) directly,
without any hook for progress reporting. This wrapper sits in between and
updates a ``tqdm`` bar on every ``read``/``readinto``/``seek`` call, so the
user gets live feedback while large files are downloaded from the source and
streamed into the target upload -- instead of the process looking stuck.
"""

from __future__ import annotations

import io

from tqdm.auto import tqdm


class ProgressStream(io.RawIOBase):
    """Wraps any readable/seekable file-like object with a tqdm progress bar."""

    def __init__(self, inner, total: int, desc: str) -> None:
        super().__init__()
        self._inner = inner
        self._bar = tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=desc,
            leave=False,
        )

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._inner.tell()

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        pos = self._inner.seek(offset, whence)
        # A seek(0) rewind means the source is being re-read from scratch
        # (see RemoteReadStream) -- reset the bar to reflect that instead of
        # showing a misleading jump backwards.
        self._bar.n = pos
        self._bar.refresh()
        return pos

    def read(self, size: int = -1) -> bytes:
        data = self._inner.read(size)
        self._bar.update(len(data))
        return data

    def readinto(self, b) -> int:  # type: ignore[override]
        n = self._inner.readinto(b)
        self._bar.update(n)
        return n

    def close(self) -> None:
        self._bar.close()
        self._inner.close()
        super().close()
