from hf_sync.providers.base import FileMeta, Provider
from hf_sync.providers.hf_provider import HFProvider
from hf_sync.providers.ms_provider import MSProvider

__all__ = ["FileMeta", "Provider", "HFProvider", "MSProvider"]


def get_provider(platform: str, token: str | None) -> Provider:
    if platform == "hf":
        return HFProvider(token=token)
    if platform == "ms":
        return MSProvider(token=token)
    raise ValueError(f"Unknown platform '{platform}'")
