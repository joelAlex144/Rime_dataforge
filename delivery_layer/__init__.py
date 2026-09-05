"""Delivery-aware position layer: know what the listener actually heard."""
from .events import EventLog
from .normalize import normalize, normalize_with_map, Segment
from .wordmap import WordMap, WordSpan, build_word_map

__all__ = ["EventLog", "normalize", "normalize_with_map", "Segment", "WordMap", "WordSpan", "build_word_map"]
