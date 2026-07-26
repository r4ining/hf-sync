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
import threading

from tqdm.auto import tqdm

# Thread-local registry of the tqdm row "position" assigned to the current
# worker thread's in-flight file transfer. Provider upload code (e.g.
# ms_provider.py's ModelScope SDK call) creates its own tqdm bar internally
# for the upload phase and has no way to accept a `position` argument from
# us. By recording the position here before calling into the provider, and
# monkeypatching tqdm to read it as a default, that SDK-created bar ends up
# on the same row as our own download bar for that file instead of always
# defaulting to row 0 and colliding with other concurrent transfers.
_position_local = threading.local()


def set_current_position(position: int | None) -> None:
    _position_local.value = position


def get_current_position() -> int | None:
    return getattr(_position_local, "value", None)


class ProgressStream(io.RawIOBase):
    """Wraps any readable/seekable file-like object with a tqdm progress bar."""

    def __init__(self, inner, total: int, desc: str, position: int | None = None) -> None:
        super().__init__()
        self._inner = inner
        self._position = position
        self._seek_count = 0
        self._bar: tqdm | None = tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=desc,
            leave=False,
            position=position,
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
        prev_n = self._bar.n if self._bar is not None else 0
        pos = self._inner.seek(offset, whence)
        if self._bar is not None:
            self._bar.n = pos
            # When the upload SDK rewinds to the start (seek(0)) after a
            # substantial read pass, switch the label from download (↓) to
            # upload (↑) so the user can distinguish hash-computation from
            # the actual upload. We require the *previous* pass to have
            # read a non-trivial amount (not just a tiny probe) before
            # toggling: huggingface_hub's UploadInfo.from_fileobj() does a
            # `read(512)` sample + `seek(0)` *before* the real hash pass, and
            # naively toggling on that first, tiny seek(0) would mislabel
            # the subsequent full hash-computation read as "upload".
            if pos == 0 and self._seek_count == 0 and prev_n > 4096:
                self._seek_count += 1
                old_desc = self._bar.desc or ""
                if "↓" in old_desc:
                    self._bar.desc = old_desc.replace("↓", "↑")
                    self._bar.refresh()
            else:
                self._bar.refresh()
        return pos

    def read(self, size: int = -1) -> bytes:
        data = self._inner.read(size)
        if self._bar is not None:
            self._bar.update(len(data))
        return data

    def readinto(self, b) -> int:  # type: ignore[override]
        n = self._inner.readinto(b)
        if self._bar is not None:
            self._bar.update(n)
        return n

    def close(self) -> None:
        if self._bar is None:
            return
        leave = self._bar.leave
        self._bar.close()
        if not leave and self._position is None:
            print()
        self._bar = None
        self._inner.close()
        super().close()
