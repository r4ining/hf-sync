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

import logging
import os
import shutil
import sys
import tempfile
from typing import List, Optional
from urllib.parse import urlparse

import requests

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


class MSProvider(Provider):
    name = "ms"

    def __init__(self, token: Optional[str] = None, endpoint: str = _MS_ENDPOINT) -> None:
        super().__init__(token=token)
        self.endpoint = endpoint.rstrip("/")
        self._hub_api = None  # lazily constructed; import is somewhat heavy
        self._session = self._build_session()

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
        headers = self._headers()
        params = {"Revision": revision, "FilePath": path}

        def opener() -> requests.Response:
            return self._session.get(url, params=params, headers=headers, stream=True, allow_redirects=True)

        return RemoteReadStream(opener)

    def upload(
        self,
        repo_id: str,
        repo_type: str,
        revision: str,
        path_in_repo: str,
        stream: RemoteReadStream,
        size: int,
        commit_message: str,
    ) -> None:
        if size > _LARGE_FILE_SPOOL_THRESHOLD:
            self._upload_via_temp_file(repo_id, repo_type, revision, path_in_repo, stream, commit_message)
            return
        self.hub_api.upload_file(
            repo_id=repo_id,
            repo_type=repo_type,
            path_or_fileobj=stream,
            path_in_repo=path_in_repo,
            revision=revision,
            commit_message=commit_message,
            disable_tqdm=True,
        )

    def _upload_via_temp_file(
        self,
        repo_id: str,
        repo_type: str,
        revision: str,
        path_in_repo: str,
        stream: RemoteReadStream,
        commit_message: str,
    ) -> None:
        tmp = tempfile.NamedTemporaryFile(prefix="hf-sync-", suffix=".part", delete=False)
        tmp_path = tmp.name
        try:
            with tmp:
                shutil.copyfileobj(stream, tmp, length=16 * 1024 * 1024)
            # The ProgressStream wrapping `stream` has a tqdm bar on stderr
            # that is now at 100% but not yet closed (close() happens in
            # sync.py's finally block). Without a newline, the log line below
            # would stick to the end of the progress bar.
            sys.stderr.write("\n")
            sys.stderr.flush()
            logger.info("Uploading %s to ModelScope from local temp file ...", path_in_repo)
            self.hub_api.upload_file(
                repo_id=repo_id,
                repo_type=repo_type,
                path_or_fileobj=tmp_path,
                path_in_repo=path_in_repo,
                revision=revision,
                commit_message=commit_message,
                disable_tqdm=True,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

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
