"""Resume-point helper: sentence boundary before the cut, plus re-entry cue.

Design decision (README §10): resume at the start of the sentence containing
the delivery boundary, never replay the whole clause, and prefix a short cue.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResumePoint:
    unit_id: str
    sentence_index: int
    char_start: int
    text: str          # display text from the resume point to the end of the unit
    cue: str           # spoken re-entry cue

    @property
    def spoken_prefix(self) -> str:
        return f"{self.cue} "


def resume_point(unit_id: str, text_display: str, sentences: list[list[int]], cut_char: int,
                 section_title: str = "") -> ResumePoint:
    """`cut_char` is the delivery boundary (WordMap.offset_at). Returns the
    sentence that contains it. If the boundary sits exactly on a sentence end,
    resume at the *next* sentence — that sentence was fully heard."""
    idx = len(sentences) - 1
    for i, (s, e) in enumerate(sentences):
        if cut_char < e:
            idx = i
            break
    else:
        idx = len(sentences)  # whole unit heard
    if idx >= len(sentences):
        return ResumePoint(unit_id, idx, len(text_display), "", "")
    s, e = sentences[idx]
    cue = "So —" if idx == 0 else "Picking up —"
    if section_title and idx == 0:
        cue = f"So — back to {section_title.lower()}."
    return ResumePoint(unit_id, idx, s, text_display[s:], cue)
