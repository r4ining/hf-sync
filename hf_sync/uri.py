"""Parsing of ``hf:<repo>`` / ``ms:<repo>`` style repository references."""

from __future__ import annotations

from dataclasses import dataclass

_SCHEMES = {
    "hf": "hf",
    "huggingface": "hf",
    "ms": "ms",
    "modelscope": "ms",
}


@dataclass(frozen=True)
class RepoRef:
    """A parsed reference such as ``hf:org/repo`` or ``ms:org/repo``."""

    platform: str  # "hf" or "ms"
    repo_id: str

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.platform}:{self.repo_id}"


def parse_repo_ref(raw: str) -> RepoRef:
    """Parse a ``<scheme>:<repo_id>`` string into a :class:`RepoRef`.

    Examples
    --------
    >>> parse_repo_ref("hf:meta-llama/Llama-3-8B")
    RepoRef(platform='hf', repo_id='meta-llama/Llama-3-8B')
    >>> parse_repo_ref("ms:qwen/Qwen2-7B")
    RepoRef(platform='ms', repo_id='qwen/Qwen2-7B')
    """
    if ":" not in raw:
        raise ValueError(
            f"Invalid repo reference '{raw}'. Expected format '<hf|ms>:<namespace>/<repo>'."
        )
    scheme, repo_id = raw.split(":", 1)
    scheme = scheme.strip().lower()
    repo_id = repo_id.strip()
    if scheme not in _SCHEMES:
        raise ValueError(
            f"Unknown scheme '{scheme}' in '{raw}'. Supported schemes: hf, ms."
        )
    if not repo_id:
        raise ValueError(f"Missing repo id in '{raw}'.")
    return RepoRef(platform=_SCHEMES[scheme], repo_id=repo_id)
