"""Local filesystem provider.

Lets ``hf-sync sync`` be used as a plain download/upload tool by treating a
local directory as just another "repo" endpoint: ``hf:<repo>`` <-> local dir,
or local dir <-> ``ms:<repo>``. Reading a file returns a regular file handle
(seekable, so remote SDKs that need to rewind for hashing work unmodified),
and writing a file streams straight into its final destination path -- no
temp directory or staging area is used, per the "sync directly to the target
local dir" requirement.
"""

from __future__ import annotations

import os
import shutil
from typing import IO, List, Optional

from hf_sync.providers.base import FileMeta, Provider


class LocalProvider(Provider):
    name = "local"

    def repo_exists(self, repo_id: str, repo_type: str) -> bool:
        return os.path.isdir(repo_id)

    def ensure_repo(self, repo_id: str, repo_type: str, private: bool = False) -> None:
        os.makedirs(repo_id, exist_ok=True)

    def list_files(self, repo_id: str, repo_type: str, revision: str) -> List[FileMeta]:
        if not os.path.isdir(repo_id):
            return []
        files: List[FileMeta] = []
        for dirpath, _dirnames, filenames in os.walk(repo_id):
            for filename in filenames:
                full_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(full_path, repo_id).replace(os.sep, "/")
                size = os.path.getsize(full_path)
                files.append(FileMeta(path=rel_path, size=size, sha256=None))
        return files

    def open_read_stream(self, repo_id: str, repo_type: str, revision: str, path: str) -> IO[bytes]:
        full_path = os.path.join(repo_id, path)
        return open(full_path, "rb")

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
        full_path = os.path.join(repo_id, path_in_repo)
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        tmp_path = full_path + ".part"
        with open(tmp_path, "wb") as out:
            shutil.copyfileobj(stream, out, length=16 * 1024 * 1024)
        os.replace(tmp_path, full_path)

    def delete_files(
        self,
        repo_id: str,
        repo_type: str,
        revision: str,
        paths: List[str],
        commit_message: str,
    ) -> None:
        for path in paths:
            full_path = os.path.join(repo_id, path)
            try:
                os.remove(full_path)
            except OSError:
                pass
