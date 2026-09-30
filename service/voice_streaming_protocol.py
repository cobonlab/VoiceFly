"""Protocol state for privacy-safe, stable streaming dictation updates."""

from collections import deque
from dataclasses import dataclass


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def common_prefix_length(left, right):
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def common_prefix(strings):
    values = tuple(strings)
    if not values:
        return ""
    prefix = values[0]
    for value in values[1:]:
        prefix = prefix[:common_prefix_length(prefix, value)]
        if not prefix:
            break
    return prefix


@dataclass(frozen=True)
class PartialPresentation:
    text: str
    sequence: int
    stable_utf16: int
    revision_utf16: int
    stable_rollback: bool


class PartialStabilityTracker:
    """Estimate a conservative stable prefix without delaying current text.

    The visible text is always the newest recognizer hypothesis.  A prefix is
    advertised as stable only after it survived several *different* published
    hypotheses and is separated from the changing tail.  If the recognizer
    still revises that prefix, the boundary is rolled back and measured; text
    is never hidden merely to preserve a stale stability claim.
    """

    def __init__(self, observation_window=3, mutable_tail_codepoints=4):
        if observation_window < 2:
            raise ValueError("observation_window must be at least 2")
        if mutable_tail_codepoints < 0:
            raise ValueError("mutable_tail_codepoints must be non-negative")
        self.observation_window = observation_window
        self.mutable_tail_codepoints = mutable_tail_codepoints
        self._history = deque(maxlen=observation_window)
        self._stable_prefix = ""
        self._last_text = ""
        self.sequence = 0
        self.rollback_count = 0

    def observe(self, text, *, stable_limit_codepoints=None):
        if not isinstance(text, str):
            raise TypeError("partial text must be str")
        if (
            stable_limit_codepoints is not None
            and (
                type(stable_limit_codepoints) is not int
                or not 0 <= stable_limit_codepoints <= len(text)
            )
        ):
            raise ValueError("stable_limit_codepoints must be a valid text offset")
        self.sequence += 1
        revision_codepoints = common_prefix_length(self._last_text, text)
        self._history.append(text)

        candidate = ""
        if len(self._history) == self.observation_window:
            agreed = common_prefix(self._history)
            stable_count = max(0, len(agreed) - self.mutable_tail_codepoints)
            if stable_limit_codepoints is not None:
                stable_count = min(stable_count, stable_limit_codepoints)
            candidate = agreed[:stable_count]

        rollback = bool(self._stable_prefix and not text.startswith(self._stable_prefix))
        if (
            stable_limit_codepoints is not None
            and len(self._stable_prefix) > stable_limit_codepoints
        ):
            self._stable_prefix = self._stable_prefix[:stable_limit_codepoints]
            rollback = True
        if rollback:
            retained = common_prefix_length(self._stable_prefix, text)
            self._stable_prefix = self._stable_prefix[:retained]
            self.rollback_count += 1
        if text.startswith(self._stable_prefix) and len(candidate) > len(self._stable_prefix):
            self._stable_prefix = candidate

        self._last_text = text
        return PartialPresentation(
            text=text,
            sequence=self.sequence,
            stable_utf16=utf16_length(self._stable_prefix),
            revision_utf16=utf16_length(text[:revision_codepoints]),
            stable_rollback=rollback,
        )


@dataclass(frozen=True)
class RevisionAttribution:
    raw_revision_utf16: int
    raw_rollback: bool
    presentation_reclassification: bool


class RevisionAttributionTracker:
    """Separate recognizer revisions from deterministic presentation changes."""

    def __init__(self):
        self._last_raw = ""
        self._last_presentation = ""
        self.raw_rollback_count = 0
        self.presentation_reclassification_count = 0

    def observe(self, raw_text, presentation_text):
        if not isinstance(raw_text, str) or not isinstance(presentation_text, str):
            raise TypeError("revision inputs must be strings")
        raw_revision = common_prefix_length(self._last_raw, raw_text)
        raw_rollback = bool(
            self._last_raw
            and raw_text != self._last_raw
            and not raw_text.startswith(self._last_raw)
        )
        presentation_reclassification = bool(
            self._last_raw
            and raw_text.startswith(self._last_raw)
            and self._last_presentation
            and not presentation_text.startswith(self._last_presentation)
        )
        if raw_rollback:
            self.raw_rollback_count += 1
        if presentation_reclassification:
            self.presentation_reclassification_count += 1
        self._last_raw = raw_text
        self._last_presentation = presentation_text
        return RevisionAttribution(
            raw_revision_utf16=utf16_length(raw_text[:raw_revision]),
            raw_rollback=raw_rollback,
            presentation_reclassification=presentation_reclassification,
        )
