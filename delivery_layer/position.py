"""
Position manager.

Holds two distinct notions of "where we are," per the architecture doc:

  - read position: where the document reader was / should resume
  - reference position: where an answer's grounding evidence lives

These are NOT the same variable. A question interrupts the read at some
point, gets answered from wherever in the document grounds that answer
(which may be a different clause entirely), and then reading must
resume from the read position -- without ever advancing the "current
position" the user perceives past where they actually stopped.

It is a STACK, not a single pair of variables, because a question can
itself be interrupted before it's answered (nested interruption): the
listener asks "what does that mean", and before the answer finishes,
interrupts again to ask a *different* question. Each level of nesting
needs its own saved read position to unwind back to.

Resume anchor rule (per spec): resume at the sentence boundary *before*
the cut point, not the exact cut point and not the whole paragraph. A
short re-entry cue is prepended so the resumption doesn't feel abrupt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Templates keyed loosely by whether we're resuming after an answer vs.
# after a plain interruption with no question asked. {clause_label} is
# filled from the fixture's human-readable section label if available,
# else falls back to the unit_id.
REENTRY_CUE_TEMPLATES = {
    "after_answer": "So — back to {clause_label}...",
    "after_interrupt_no_answer": "Continuing with {clause_label}...",
    "default": "Picking back up at {clause_label}...",
}


@dataclass(frozen=True)
class ResumeAnchor:
    """Where reading should resume from: a specific unit, and the
    character offset within its text_display marking the sentence
    boundary before the cut (not the raw cut offset)."""

    unit_id: str
    char_offset: int  # start of the sentence to resume from
    sentence_text: str  # the sentence text itself, for the re-entry cue / logging


@dataclass(frozen=True)
class PositionFrame:
    """One level of the position stack. Pushed when an interruption
    happens; popped when that interruption's answer (or non-answer) is
    fully delivered and reading resumes."""

    turn_id: int
    read_unit_id: str
    resume_anchor: ResumeAnchor
    deictic_anchor_unit_id: Optional[str] = None  # "that" / "it" resolves here
    reference_unit_id: Optional[str] = None  # where the answer is grounded, once known


class PositionManager:
    """One instance per session. Not thread-safe -- runs on the same
    event loop as the rest of the agent."""

    def __init__(self, ledger=None) -> None:
        self._stack: list[PositionFrame] = []
        self._ledger = ledger  # optional, for position_saved/restored events

    # -- stack depth / inspection ------------------------------------------

    @property
    def depth(self) -> int:
        return len(self._stack)

    @property
    def current(self) -> Optional[PositionFrame]:
        return self._stack[-1] if self._stack else None

    # -- sentence-boundary resolution ---------------------------------------

    @staticmethod
    def resolve_resume_anchor(
        unit_id: str, text_display: str, cut_char_offset: int
    ) -> ResumeAnchor:
        """Find the sentence boundary at or before cut_char_offset.
        Simple heuristic: scan backward from the cut point for the
        nearest sentence-ending punctuation (. ! ?) followed by
        whitespace, and resume from the sentence that starts after it.
        If no such boundary exists before the cut, resume from the
        start of the unit -- never mid-sentence, per spec ("never
        replay the whole paragraph" cuts the other way too: don't
        resume mid-word either).
        """
        if cut_char_offset <= 0 or not text_display:
            return ResumeAnchor(unit_id=unit_id, char_offset=0, sentence_text=text_display)

        search_region = text_display[:cut_char_offset]
        boundary = 0
        for i, ch in enumerate(search_region):
            if ch in ".!?" and i + 1 < len(search_region) and search_region[i + 1] in " \n\t":
                boundary = i + 1
        # skip leading whitespace after the boundary
        start = boundary
        while start < len(text_display) and text_display[start] in " \n\t":
            start += 1

        # sentence_text: from start up to the next sentence-ending
        # punctuation (or end of unit), for logging / the re-entry cue.
        end = len(text_display)
        for i in range(start, len(text_display)):
            if text_display[i] in ".!?":
                end = i + 1
                break
        sentence_text = text_display[start:end].strip()

        return ResumeAnchor(unit_id=unit_id, char_offset=start, sentence_text=sentence_text)

    # -- push / pop -----------------------------------------------------------

    def push_interruption(
        self,
        *,
        turn_id: int,
        read_unit_id: str,
        text_display: str,
        cut_char_offset: int,
        deictic_anchor_unit_id: Optional[str] = None,
    ) -> PositionFrame:
        """Called when an interruption is detected mid-read. Saves
        exactly where we were so we can get back to it, regardless of
        how deep the question-answering goes."""
        anchor = self.resolve_resume_anchor(read_unit_id, text_display, cut_char_offset)
        frame = PositionFrame(
            turn_id=turn_id,
            read_unit_id=read_unit_id,
            resume_anchor=anchor,
            deictic_anchor_unit_id=deictic_anchor_unit_id or read_unit_id,
        )
        self._stack.append(frame)
        if self._ledger is not None:
            self._ledger.log_position_saved(
                turn_id=turn_id, unit_id=read_unit_id, anchor=anchor.sentence_text
            )
        return frame

    def set_reference_unit(self, reference_unit_id: str) -> None:
        """Records where the answer to the current (top-of-stack)
        question is grounded, distinct from where reading resumes."""
        if not self._stack:
            raise RuntimeError("set_reference_unit called with an empty position stack")
        top = self._stack[-1]
        self._stack[-1] = PositionFrame(
            turn_id=top.turn_id,
            read_unit_id=top.read_unit_id,
            resume_anchor=top.resume_anchor,
            deictic_anchor_unit_id=top.deictic_anchor_unit_id,
            reference_unit_id=reference_unit_id,
        )

    def pop_and_resume(self, *, answered: bool, clause_label: Optional[str] = None) -> PositionFrame:
        """Called once the current interruption's answer (or silence,
        if no question was actually asked) has been fully delivered.
        Returns the frame to resume from; caller is responsible for
        actually re-starting the scheduler at resume_anchor."""
        if not self._stack:
            raise RuntimeError("pop_and_resume called with an empty position stack")
        frame = self._stack.pop()
        if self._ledger is not None:
            self._ledger.log_position_restored(
                turn_id=frame.turn_id,
                unit_id=frame.resume_anchor.unit_id,
                anchor=frame.resume_anchor.sentence_text,
            )
        return frame

    def reentry_cue(self, frame: PositionFrame, *, answered: bool, clause_label: Optional[str] = None) -> str:
        """Builds the spoken re-entry line for resuming after `frame`.
        clause_label should come from the fixture's human-readable
        section label; falls back to the raw unit_id if not supplied."""
        label = clause_label or frame.resume_anchor.unit_id
        template_key = "after_answer" if answered else "after_interrupt_no_answer"
        return REENTRY_CUE_TEMPLATES.get(template_key, REENTRY_CUE_TEMPLATES["default"]).format(
            clause_label=label
        )

    def deictic_target(self) -> Optional[str]:
        """What unit does 'that' / 'it' currently resolve to -- i.e.
        the last clause actually heard before the current interruption,
        not wherever synthesis had gotten to. Callers pass this into
        Q&A grounding so answers are anchored correctly."""
        if not self._stack:
            return None
        return self._stack[-1].deictic_anchor_unit_id
