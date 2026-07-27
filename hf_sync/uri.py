"""Parsing of ``hf:<repo>`` / ``ms:<repo>`` style repository references.

A source/target that does not use a recognized ``hf:``/``ms:`` scheme prefix
is treated as a local filesystem path instead, so ``hf-sync`` can also be
used as a plain download/upload tool, e.g.::

    hf-sync sync hf:org/repo /path/to/local/dir   # download
    hf-sync sync /path/to/local/dir hf:org/repo   # upload
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_SCHEMES = {
    "hf": "hf",
    "huggingface": "hf",
    "ms": "ms",
    "modelscope": "ms",
}


@dataclass(frozen=True)
class RepoRef:
    """A parsed reference such as ``hf:org/repo``, ``ms:org/repo``, or a local path."""

    platform: str  # "hf", "ms", or "local"
    repo_id: str

    def __str__(self) -> str:  # pragma: no cover - convenience only
        if self.platform == "local":
            return self.repo_id
        return f"{self.platform}:{self.repo_id}"


def parse_repo_ref(raw: str) -> RepoRef:
    """Parse a ``<scheme>:<repo_id>`` string, or a local filesystem path.

    Examples
    --------
    >>> parse_repo_ref("hf:meta-llama/Llama-3-8B")
    RepoRef(platform='hf', repo_id='meta-llama/Llama-3-8B')
    >>> parse_repo_ref("ms:qwen/Qwen2-7B")
    RepoRef(platform='ms', repo_id='qwen/Qwen2-7B')
    >>> parse_repo_ref("/path/to/local/dir")
    RepoRef(platform='local', repo_id='/path/to/local/dir')
    """
    if ":" in raw:
        scheme, rest = raw.split(":", 1)
        scheme_lower = scheme.strip().lower()
        if scheme_lower in _SCHEMES:
            repo_id = rest.strip()
            if not repo_id:
                raise ValueError(f"Missing repo id in '{raw}'.")
            return RepoRef(platform=_SCHEMES[scheme_lower], repo_id=repo_id)
    # Not a recognized "hf:"/"ms:" scheme -- treat as a local filesystem path.
    return RepoRef(platform="local", repo_id=os.path.abspath(os.path.expanduser(raw)))
