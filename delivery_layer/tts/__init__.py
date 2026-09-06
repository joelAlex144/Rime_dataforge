from .base import AudioChunk, Done, StreamItem, Timestamps, TTSError, TTSProvider
from .fake import FakeTTS

__all__ = ["AudioChunk", "Done", "StreamItem", "Timestamps", "TTSError", "TTSProvider", "FakeTTS", "make_provider"]


def make_provider(events=None):
    """Rime unless TTS_PROVIDER=fake. The choice is always logged via provider_active."""
    import os
    if os.environ.get("TTS_PROVIDER", "rime").lower() == "fake":
        # FAKE_REALTIME=1 paces the fake at wall-clock speed so an interrupt can
        # land mid-unit. Off by default: the test suite wants it instant.
        return FakeTTS(
            events,
            realtime=os.environ.get("FAKE_REALTIME", "").strip() in ("1", "true", "yes"),
            tone_hz=float(os.environ.get("FAKE_TONE_HZ", "0") or 0) or None,
        )
    from .rime import RimeConfig, RimeTTS
    return RimeTTS(RimeConfig.from_env(), events)
