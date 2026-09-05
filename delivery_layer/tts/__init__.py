from .base import AudioChunk, Done, StreamItem, Timestamps, TTSError, TTSProvider
from .fake import FakeTTS

__all__ = ["AudioChunk", "Done", "StreamItem", "Timestamps", "TTSError", "TTSProvider", "FakeTTS", "make_provider"]


def make_provider(events=None):
    """Rime unless TTS_PROVIDER=fake. The choice is always logged via provider_active."""
    import os
    if os.environ.get("TTS_PROVIDER", "rime").lower() == "fake":
        return FakeTTS(events)
    from .rime import RimeConfig, RimeTTS
    return RimeTTS(RimeConfig.from_env(), events)
