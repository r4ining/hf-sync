from hf_sync.providers.base import FileMeta, Provider
from hf_sync.providers.hf_provider import HFProvider
from hf_sync.providers.local_provider import LocalProvider
from hf_sync.providers.ms_provider import MSProvider

__all__ = ["FileMeta", "Provider", "HFProvider", "MSProvider", "LocalProvider"]


def get_provider(platform: str, token: str | None) -> Provider:
    if platform == "hf":
        return HFProvider(token=token)
    if platform == "ms":
        return MSProvider(token=token)
    if platform == "local":
        return LocalProvider(token=token)
    raise ValueError(f"Unknown platform '{platform}'")
