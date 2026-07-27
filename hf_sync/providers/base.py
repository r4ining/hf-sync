"""Common abstractions shared by the Hugging Face and ModelScope providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import IO, List, Optional


@dataclass
class FileMeta:
    """Metadata for a single file inside a repository."""

    path: str
    size: int
    sha256: Optional[str] = None  # lowercase hex, when known without downloading


class Provider(ABC):
    """Abstract interface implemented by :class:`HFProvider` and :class:`MSProvider`."""

    name: str

    def __init__(self, token: Optional[str] = None) -> None:
        self.token = token

    @abstractmethod
    def list_files(self, repo_id: str, repo_type: str, revision: str) -> List[FileMeta]:
        """List all files in a repo. Returns an empty list if the repo does not exist."""

    @abstractmethod
    def repo_exists(self, repo_id: str, repo_type: str) -> bool:
        ...

    @abstractmethod
    def ensure_repo(self, repo_id: str, repo_type: str, private: bool = False) -> None:
        """Create the target repo if it does not already exist."""

    @abstractmethod
    def open_read_stream(self, repo_id: str, repo_type: str, revision: str, path: str) -> IO[bytes]:
        """Return a re-openable, seekable stream that reads ``path`` from the repo, on demand."""

    @abstractmethod
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
        """Upload ``stream`` (already positioned at offset 0) to ``path_in_repo``."""

    @abstractmethod
    def delete_files(
        self,
        repo_id: str,
        repo_type: str,
        revision: str,
        paths: List[str],
        commit_message: str,
    ) -> None:
        """Delete the given paths from the repo."""
