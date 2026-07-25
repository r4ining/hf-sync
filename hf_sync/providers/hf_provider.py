"""Hugging Face Hub provider."""

from __future__ import annotations

from typing import List, Optional

import requests
from huggingface_hub import CommitOperationDelete, HfApi
from huggingface_hub.hf_api import RepoFile
from huggingface_hub.utils import (
    EntryNotFoundError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
    disable_progress_bars,
)

from hf_sync.providers.base import FileMeta, Provider
from hf_sync.remote_stream import RemoteReadStream

_HF_ENDPOINT = "https://huggingface.co"

# huggingface_hub shows its own tqdm progress bar for uploads/downloads by
# default. We already render a single unified progress bar per file via
# hf_sync.progress.ProgressStream, so disable the SDK's own bars to avoid two
# competing tqdm instances fighting over the terminal line.
disable_progress_bars()


class HFProvider(Provider):
    name = "hf"

    def __init__(self, token: Optional[str] = None, endpoint: str = _HF_ENDPOINT) -> None:
        super().__init__(token=token)
        self.endpoint = endpoint.rstrip("/")
        self.api = HfApi(endpoint=self.endpoint, token=token)

    def repo_exists(self, repo_id: str, repo_type: str) -> bool:
        try:
            self.api.repo_info(repo_id=repo_id, repo_type=repo_type)
            return True
        except RepositoryNotFoundError:
            return False

    def ensure_repo(self, repo_id: str, repo_type: str, private: bool = False) -> None:
        self.api.create_repo(repo_id=repo_id, repo_type=repo_type, private=private, exist_ok=True)

    def list_files(self, repo_id: str, repo_type: str, revision: str) -> List[FileMeta]:
        try:
            tree = self.api.list_repo_tree(
                repo_id=repo_id,
                repo_type=repo_type,
                revision=revision,
                recursive=True,
                expand=False,
            )
        except (RepositoryNotFoundError, RevisionNotFoundError):
            return []

        files: List[FileMeta] = []
        for entry in tree:
            if not isinstance(entry, RepoFile):
                continue  # skip directories
            sha256 = entry.lfs.sha256 if entry.lfs is not None else None
            files.append(FileMeta(path=entry.path, size=entry.size, sha256=sha256))
        return files

    def _resolve_url(self, repo_id: str, repo_type: str, revision: str, path: str) -> str:
        prefix = {"model": "", "dataset": "datasets/", "space": "spaces/"}[repo_type]
        return f"{self.endpoint}/{prefix}{repo_id}/resolve/{revision}/{path}"

    def open_read_stream(self, repo_id: str, repo_type: str, revision: str, path: str) -> RemoteReadStream:
        url = self._resolve_url(repo_id, repo_type, revision, path)
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}

        def opener() -> requests.Response:
            resp = requests.get(url, headers=headers, stream=True, allow_redirects=True)
            return resp

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
        self.api.upload_file(
            path_or_fileobj=stream,
            path_in_repo=path_in_repo,
            repo_id=repo_id,
            repo_type=repo_type,
            revision=revision,
            commit_message=commit_message,
        )

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
        self.api.create_commit(
            repo_id=repo_id,
            repo_type=repo_type,
            revision=revision,
            operations=[CommitOperationDelete(path_in_repo=p) for p in paths],
            commit_message=commit_message,
        )
