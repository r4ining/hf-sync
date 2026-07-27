"""ModelScope Hub provider.

Listing and raw file download go through ModelScope's plain REST API
directly (documented informally, used by their own official clients such as
the ``modelscope`` CLI and community integrations), since it lets us stream
bytes without depending on SDK version-specific internals.

Repo creation and upload are delegated to the official ``modelscope`` SDK
(``modelscope.hub.api.HubApi``), which knows how to talk to ModelScope's
git-lfs-like storage backend and accepts a file-like object via
``path_or_fileobj`` -- exactly what :class:`~hf_sync.remote_stream.RemoteReadStream`
provides.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
from typing import IO, List, Optional
from urllib.parse import urlparse

import requests
from tqdm.auto import tqdm

from hf_sync.progress import ProgressStream, get_current_position
from hf_sync.providers.base import FileMeta, Provider
from hf_sync.remote_stream import RemoteReadStream

_MS_ENDPOINT = "https://modelscope.cn"

_REPO_TYPE_SEGMENT = {"model": "models", "dataset": "datasets"}

logger = logging.getLogger("hf_sync")

# modelscope_hub's upload_file() calls ``path_or_fileobj.read()`` in a single
# shot to compute the content hash whenever it's given anything other than a
# str/Path/bytes (see modelscope_hub._upload._compute_file_hash) -- it does
# NOT chunk-read a file-like object. For large files this fully buffers the
# entire file in memory (and keeps that buffer around for the upload itself),
# which can OOM-kill the process. Above this size we spool through a local
# temp file instead, so the SDK takes its disk-based, chunked-hashing path.
_LARGE_FILE_SPOOL_THRESHOLD = 256 * 1024 * 1024  # 256 MiB

# Large files are spooled into this directory instead of a random tempfile
# so that a partially-downloaded ``.part`` file survives an interrupted
# sync run (Ctrl-C, network failure, process crash, etc.) and can be resumed
# -- via an HTTP Range request against the source -- the next time the same
# file is synced, instead of re-downloading it from scratch.
_PARTIAL_CACHE_DIR = os.path.join(tempfile.gettempdir(), "hf-sync-partial")


def _unwrap_remote_stream(stream: IO[bytes]) -> Optional[RemoteReadStream]:
    """Reach through BufferedReader(ProgressStream(RemoteReadStream(...))) wrapping."""
    raw = getattr(stream, "raw", stream)
    inner = getattr(raw, "_inner", None)
    return inner if isinstance(inner, RemoteReadStream) else None


def _unwrap_progress_stream(stream: IO[bytes]) -> Optional[ProgressStream]:
    raw = getattr(stream, "raw", stream)
    return raw if isinstance(raw, ProgressStream) else None


_COMMIT_LOCK_MAX_RETRIES = 5
_COMMIT_LOCK_BASE_DELAY = 2  # seconds

# ModelScope does not allow concurrent commits to the same repo. When
# multiple threads upload simultaneously, the server returns a 429
# "commit lock busy" error. This lock serializes the commit phase across
# all concurrent uploads so that only one thread commits at a time, while
# the actual data transfer (download from source + upload to ModelScope
# staging) can still proceed in parallel.
_commit_lock = threading.Lock()

# The ModelScope SDK's upload_file() creates its own tqdm progress bar
# internally for the actual upload phase (after our own ProgressStream has
# already finished the download-to-temp-file phase), but it hardcodes the
# bar with no ``position`` argument -- so it always renders on row 0 and
# stomps on other concurrent transfers' bars. Since the SDK gives us no hook
# to pass a position, we patch tqdm's constructor (once, process-wide) to
# fall back to the current worker thread's assigned row -- recorded via
# ``hf_sync.progress.set_current_position()`` -- whenever the caller doesn't
# specify one explicitly. This only affects bars created without an
# explicit ``position``/``leave``, so our own ProgressStream bars (which
# always pass ``position`` explicitly) are unaffected.
_tqdm_patch_lock = threading.Lock()
_tqdm_patched = False


def _ensure_tqdm_position_patch() -> None:
    global _tqdm_patched
    if _tqdm_patched:
        return
    with _tqdm_patch_lock:
        if _tqdm_patched:
            return
        original_init = tqdm.__init__

        def _patched_init(self, *args, **kwargs):
            if kwargs.get("position") is None:
                pos = get_current_position()
                if pos is not None:
                    kwargs["position"] = pos
                    kwargs.setdefault("leave", False)
            original_init(self, *args, **kwargs)

        tqdm.__init__ = _patched_init
        _tqdm_patched = True


class MSProvider(Provider):
    name = "ms"

    def __init__(self, token: Optional[str] = None, endpoint: str = _MS_ENDPOINT) -> None:
        super().__init__(token=token)
        self.endpoint = endpoint.rstrip("/")
        self._hub_api = None  # lazily constructed; import is somewhat heavy
        self._session = self._build_session()
        _ensure_tqdm_position_patch()

    def _build_session(self) -> requests.Session:
        # ModelScope's legacy /api/v1/ endpoints (used for repo existence
        # checks, file listing, and raw file download) authenticate private
        # repos via an ``m_session_id`` cookie set to the token, *in addition*
        # to the ``Authorization: Bearer`` header. Sending only the header
        # (as we used to) silently behaves as an anonymous/public request,
        # which made private repos look like they didn't exist.
        session = requests.Session()
        if self.token:
            domain = urlparse(self.endpoint).hostname or ""
            session.cookies.set("m_session_id", self.token, domain=domain, path="/")
        return session

    @property
    def hub_api(self):
        if self._hub_api is None:
            from modelscope.hub.api import HubApi

            self._hub_api = HubApi(token=self.token)
        return self._hub_api

    def _headers(self) -> dict:
        headers = {"User-Agent": "hf-sync/0.1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def repo_exists(self, repo_id: str, repo_type: str) -> bool:
        segment = _REPO_TYPE_SEGMENT[repo_type]
        url = f"{self.endpoint}/api/v1/{segment}/{repo_id}"
        resp = self._session.get(url, headers=self._headers())
        return resp.status_code == 200

    def ensure_repo(self, repo_id: str, repo_type: str, private: bool = False) -> None:
        if self.repo_exists(repo_id, repo_type):
            return
        try:
            self.hub_api.create_repo(repo_id=repo_id, repo_type=repo_type, private=private)
        except TypeError:
            # Older/newer SDK signatures differ slightly; fall back to the
            # legacy model-only helper when repo_type isn't accepted.
            if repo_type == "model":
                self.hub_api.create_model(model_id=repo_id, visibility=5 if not private else 1)
            else:
                raise

    def list_files(self, repo_id: str, repo_type: str, revision: str) -> List[FileMeta]:
        segment = _REPO_TYPE_SEGMENT[repo_type]
        # Models/studios list via ".../repo/files"; datasets use ".../repo/tree"
        # instead -- the "files" endpoint returns 405 Method Not Allowed for
        # dataset repos.
        suffix = "repo/tree" if repo_type == "dataset" else "repo/files"
        url = f"{self.endpoint}/api/v1/{segment}/{repo_id}/{suffix}"

        files: List[FileMeta] = []
        page_size = 100
        page_number = 1
        while True:
            resp = self._session.get(
                url,
                params={
                    "Revision": revision,
                    "Recursive": "True",
                    "PageSize": page_size,
                    "PageNumber": page_number,
                },
                headers=self._headers(),
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            payload = resp.json()
            raw_files = self._extract_files(payload)

            for entry in raw_files:
                entry_type = entry.get("Type") or entry.get("type") or "blob"
                if entry_type == "tree":
                    continue
                path = entry.get("Path") or entry.get("path") or entry.get("Name")
                if not path:
                    continue
                size = int(entry.get("Size") or entry.get("size") or 0)
                sha256 = entry.get("Sha256") or entry.get("sha256") or None
                files.append(FileMeta(path=path, size=size, sha256=sha256))

            # ModelScope paginates the tree/files endpoint. Check whether
            # there are more pages to fetch.
            total_count = None
            if isinstance(payload, dict):
                total_count = payload.get("TotalCount")
                if total_count is None and isinstance(payload.get("Data"), dict):
                    total_count = payload["Data"].get("TotalCount")
            if total_count is not None and len(files) < total_count and len(raw_files) > 0:
                page_number += 1
                continue
            break

        return files

    @staticmethod
    def _extract_files(payload) -> list:
        # Response shape varies by repo type/endpoint: a plain list, a
        # top-level {"Files": [...]}, or nested {"Data": {"Files": [...]}}.
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            if isinstance(payload.get("Files"), list):
                return payload["Files"]
            data = payload.get("Data")
            if isinstance(data, dict) and isinstance(data.get("Files"), list):
                return data["Files"]
        return []

    def _download_url(self, repo_id: str, repo_type: str, path: str) -> str:
        segment = _REPO_TYPE_SEGMENT[repo_type]
        return f"{self.endpoint}/api/v1/{segment}/{repo_id}/repo"

    def open_read_stream(self, repo_id: str, repo_type: str, revision: str, path: str) -> RemoteReadStream:
        url = self._download_url(repo_id, repo_type, path)
        base_headers = self._headers()
        params = {"Revision": revision, "FilePath": path}

        def opener(offset: int = 0) -> requests.Response:
            headers = dict(base_headers)
            if offset:
                headers["Range"] = f"bytes={offset}-"
            return self._session.get(url, params=params, headers=headers, stream=True, allow_redirects=True)

        return RemoteReadStream(opener)

    def upload(
        self,
        repo_id: str,
        repo_type: str,
        revision: str,
        path_in_repo: str,
        stream: IO[bytes],
        size: int,
        commit_message: str,
    ) -> None:
        if size > _LARGE_FILE_SPOOL_THRESHOLD:
            self._upload_via_temp_file(repo_id, repo_type, revision, path_in_repo, stream, size, commit_message)
            return
        # The SDK reads small files fully into memory anyway (it checks
        # isinstance(path_or_fileobj, io.BufferedIOBase) and calls .read())
        # before hashing + uploading. We do that read ourselves so our own
        # ProgressStream (download phase) can be closed -- freeing its
        # terminal row -- as soon as it's drained, *before* handing plain
        # bytes to the SDK. The SDK then creates its own (position-patched)
        # bar for the actual upload PUT. Without closing ours first, both
        # bars would claim the same row at the same time and corrupt the
        # terminal output.
        data = stream.read()
        stream.close()
        self._upload_with_retry(
            lambda: self.hub_api.upload_file(
                repo_id=repo_id,
                repo_type=repo_type,
                path_or_fileobj=data,
                path_in_repo=path_in_repo,
                revision=revision,
                commit_message=commit_message,
                disable_tqdm=False,
            ),
            path_in_repo,
        )

    def _partial_file_path(self, repo_id: str, repo_type: str, revision: str, path_in_repo: str, size: int) -> str:
        key = f"{repo_id}::{repo_type}::{revision}::{path_in_repo}::{size}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        os.makedirs(_PARTIAL_CACHE_DIR, exist_ok=True)
        return os.path.join(_PARTIAL_CACHE_DIR, f"{digest}.part")

    def _upload_via_temp_file(
        self,
        repo_id: str,
        repo_type: str,
        revision: str,
        path_in_repo: str,
        stream: IO[bytes],
        size: int,
        commit_message: str,
    ) -> None:
        # Stable (not random) path: an interrupted run leaves a ``.part``
        # file behind here that a later sync of the same file can resume
        # from, instead of a throwaway tempfile that's always discarded.
        tmp_path = self._partial_file_path(repo_id, repo_type, revision, path_in_repo, size)
        remote = _unwrap_remote_stream(stream)

        resume_offset = 0
        if os.path.exists(tmp_path):
            existing_size = os.path.getsize(tmp_path)
            if existing_size == size:
                # Already fully downloaded locally (likely crashed/interrupted
                # right before or during the upload step) -- skip straight to
                # uploading it, no need to touch the source again.
                logger.info(
                    "Found fully-downloaded partial file for %s, skipping re-download ...", path_in_repo,
                )
                stream.close()
                self._upload_with_retry(
                    lambda: self.hub_api.upload_file(
                        repo_id=repo_id,
                        repo_type=repo_type,
                        path_or_fileobj=tmp_path,
                        path_in_repo=path_in_repo,
                        revision=revision,
                        commit_message=commit_message,
                        disable_tqdm=False,
                    ),
                    path_in_repo,
                )
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                return
            if 0 < existing_size < size and remote is not None and remote.reopen_from(existing_size):
                resume_offset = existing_size
                progress = _unwrap_progress_stream(stream)
                if progress is not None:
                    progress.set_progress(existing_size)
                logger.info(
                    "Resuming interrupted download of %s from %s/%s bytes ...",
                    path_in_repo, f"{existing_size:,}", f"{size:,}",
                )
            else:
                # Stale, corrupt, or the source doesn't support Range
                # requests -- discard and start over from scratch.
                os.remove(tmp_path)

        mode = "ab" if resume_offset else "wb"
        with open(tmp_path, mode) as tmp:
            shutil.copyfileobj(stream, tmp, length=16 * 1024 * 1024)
        # Close the download progress bar (on the ProgressStream wrapper)
        # before starting the upload so the SDK's own upload bar doesn't
        # conflict with a stale 100% download bar on the same line.
        stream.close()
        logger.info("Uploading %s to ModelScope ...", path_in_repo)
        try:
            self._upload_with_retry(
                lambda: self.hub_api.upload_file(
                    repo_id=repo_id,
                    repo_type=repo_type,
                    path_or_fileobj=tmp_path,
                    path_in_repo=path_in_repo,
                    revision=revision,
                    commit_message=commit_message,
                    disable_tqdm=False,
                ),
                path_in_repo,
            )
        except Exception:
            # Keep the fully-downloaded partial file on disk -- a later
            # sync run can upload it directly without re-downloading.
            logger.warning(
                "Upload of %s failed; keeping local partial file so the next sync run can resume it.",
                path_in_repo,
            )
            raise
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    def _upload_with_retry(self, upload_fn, path_in_repo: str) -> None:
        """Run an upload call and retry on ModelScope's commit-lock conflicts.

        ``upload_fn`` wraps the SDK's ``upload_file()``, which does three
        things in sequence: (1) hash the local data, (2) PUT the blob to
        storage, (3) call ``create_commit`` to attach it to the repo.
        ModelScope serializes step 3 *per repo* server-side and returns a
        429 "commit lock busy" error when two commits race.

        The first attempt runs with **no client-side lock** so that step 2
        (the potentially multi-GB blob upload) can proceed fully in
        parallel across concurrent transfers -- only step 3 is actually
        contended. If we do hit a lock-busy error, subsequent retries are
        serialized via ``_commit_lock`` so that only one thread retries at a
        time, which converges much faster than every thread hammering the
        server independently. Retries are cheap even for large files: the
        SDK's blob upload step already dedups by content hash server-side
        (``_validate_blob``), so a blob that was already PUT in the failed
        attempt is not re-transferred.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(_COMMIT_LOCK_MAX_RETRIES):
            try:
                if attempt == 0:
                    upload_fn()
                else:
                    with _commit_lock:
                        upload_fn()
                return
            except Exception as exc:
                if "commit lock busy" in str(exc).lower() or "429" in str(exc):
                    last_exc = exc
                    delay = _COMMIT_LOCK_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "ModelScope commit lock busy for %s, retrying in %ds (attempt %d/%d) ...",
                        path_in_repo, delay, attempt + 1, _COMMIT_LOCK_MAX_RETRIES,
                    )
                    time.sleep(delay)
                else:
                    raise
        raise RuntimeError(
            f"ModelScope commit lock remained busy for {path_in_repo} after "
            f"{_COMMIT_LOCK_MAX_RETRIES} retries"
        ) from last_exc

    def delete_files(
        self,
        repo_id: str,
        repo_type: str,
        revision: str,
        paths: List[str],
        commit_message: str,
    ) -> None:
        if not paths:
            return
        try:
            self.hub_api.delete_files(
                repo_id=repo_id,
                repo_type=repo_type,
                paths=paths,
                revision=revision,
                commit_message=commit_message,
            )
        except TypeError:
            # Some SDK versions don't accept revision/commit_message kwargs.
            self.hub_api.delete_files(repo_id=repo_id, repo_type=repo_type, paths=paths)
        except Exception as exc:
            raise RuntimeError(
                "ModelScope 删除文件失败："
                f"{exc}. 注意：ModelScope 的 delete_files 目前要求 cookie 会话登录，"
                "API token（ms-...）可能会被拒绝（401）。可尝试用 `modelscope login` 完成一次"
                "浏览器登录后重试，或改到 ModelScope 网页控制台手动删除多余文件。"
            ) from exc
