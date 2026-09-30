import unittest

from voice_streaming_protocol import (
    PartialStabilityTracker,
    RevisionAttributionTracker,
)


class PartialStabilityTrackerTests(unittest.TestCase):
    def test_sequence_is_monotonic_and_stable_prefix_lags_mutable_tail(self):
        tracker = PartialStabilityTracker(
            observation_window=3,
            mutable_tail_codepoints=2,
        )
        updates = [
            tracker.observe(text)
            for text in ("现在", "现在有", "现在有三个", "现在有三个问题")
        ]

        self.assertEqual([item.sequence for item in updates], [1, 2, 3, 4])
        self.assertEqual([item.stable_utf16 for item in updates], [0, 0, 0, 1])
        self.assertEqual(updates[-1].revision_utf16, len("现在有三个"))

    def test_revision_rolls_back_a_wrong_stability_claim_instead_of_hiding_text(self):
        tracker = PartialStabilityTracker(
            observation_window=2,
            mutable_tail_codepoints=0,
        )
        tracker.observe("现在有三个问题")
        stable = tracker.observe("现在有三个问题吗")
        corrected = tracker.observe("现在没有问题")

        self.assertEqual(stable.stable_utf16, len("现在有三个问题"))
        self.assertTrue(corrected.stable_rollback)
        self.assertEqual(corrected.text, "现在没有问题")
        self.assertEqual(corrected.stable_utf16, len("现在"))
        self.assertEqual(tracker.rollback_count, 1)

    def test_utf16_offsets_never_use_python_codepoint_counts(self):
        tracker = PartialStabilityTracker(
            observation_window=2,
            mutable_tail_codepoints=0,
        )
        tracker.observe("A😀")
        update = tracker.observe("A😀中文")
        self.assertEqual(update.stable_utf16, 3)

    def test_pending_numeric_tail_caps_the_stable_prefix(self):
        tracker = PartialStabilityTracker(
            observation_window=2,
            mutable_tail_codepoints=0,
        )
        tracker.observe("前文 T 幺", stable_limit_codepoints=3)
        pending = tracker.observe("前文 T 幺零", stable_limit_codepoints=3)
        closed = tracker.observe("前文 T10机")

        self.assertEqual(pending.stable_utf16, 3)
        self.assertFalse(pending.stable_rollback)
        self.assertFalse(closed.stable_rollback)
        self.assertEqual(tracker.rollback_count, 0)

    def test_rejects_invalid_stable_limit(self):
        tracker = PartialStabilityTracker()
        with self.assertRaises(ValueError):
            tracker.observe("文本", stable_limit_codepoints=3)


class RevisionAttributionTrackerTests(unittest.TestCase):
    def test_distinguishes_raw_asr_rollback_from_itn_reclassification(self):
        tracker = RevisionAttributionTracker()
        tracker.observe("T 幺", "T 幺")
        itn = tracker.observe("T 幺零机", "T10机")
        raw = tracker.observe("T 幺六机", "T16机")

        self.assertTrue(itn.presentation_reclassification)
        self.assertFalse(itn.raw_rollback)
        self.assertTrue(raw.raw_rollback)
        self.assertFalse(raw.presentation_reclassification)
        self.assertEqual(tracker.presentation_reclassification_count, 1)
        self.assertEqual(tracker.raw_rollback_count, 1)


if __name__ == "__main__":
    unittest.main()
