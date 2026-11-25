import unittest

import numpy as np

from review_app.extraction import Extractor, describe_review
from review_app.radio_transcription import activity_bounds, group_fragments
from review_app.reported_times import reported_time


class ReportedTimeTests(unittest.TestCase):
    def test_revision_takes_precedence_over_receipt_and_suggests_only_matching_field(self):
        glossary = {"subjects": [
            {"subject_id": "handoff", "label": "Handoff", "value_type": "elapsed_time"},
            {"subject_id": "arrival", "label": "Arrival", "value_type": "elapsed_time"},
        ]}
        extractor = Extractor(glossary, elapsed_times=True)
        events = []
        for index, text in enumerate([
            "Handoff at 02:06:00.",
            "Arrival at 02:06:11.",
            "Roger. Handoff should be two plus oh six plus oh three. My mistake.",
            "Roger.",
        ], 1):
            proposal = extractor.extract(text)
            events.append(dict(id=index, ordinal=index, source_file_id=1, capture_offset_ms=index * 1000,
                               state="proposed", target_id=None, body=text, **vars(proposal)))
        describe_review(events)
        self.assertEqual([event["kind"] for event in events],
                         ["time_report", "time_report", "correction", "acknowledgment"])
        self.assertEqual(events[2]["reported_time_seconds"], 7563)
        self.assertEqual(events[2]["candidates"], [1])
        self.assertEqual(Extractor(glossary).extract(events[2]["body"]).kind, "acknowledgment")

    def test_invalid_or_ambiguous_times_are_unresolved(self):
        aliases = ["handoff"]
        self.assertIsNone(reported_time("Handoff at 03:15:60.", aliases))
        self.assertIsNone(reported_time("Handoff at 3:1:503.", aliases))
        self.assertIsNone(reported_time("Handoff should be 03:15:03.", aliases))
        self.assertIsNone(reported_time("Handoff at 03:15:03 or handoff at 03:15:00.", aliases))
        self.assertEqual(reported_time("Roger. Handoff at 3 plus 1 5 plus 0 3.", aliases), (11703, False))
        self.assertEqual(reported_time("Roger. That handoff should be 3 plus 1-5-0-3.", aliases), (11703, True))

    def test_radio_reply_stays_separate_from_fragments_inside_a_readback(self):
        cues = group_fragments([(1, 3.0, "Roger. I have the first value."),
                                (3.7, 4.5, "And the second."), (4.9, 5.6, "Roger, standby.")])
        self.assertEqual(cues, [(1, 4.5, "Roger. I have the first value. And the second."),
                                (4.9, 5.6, "Roger, standby.")])
        signal = np.zeros(16000)
        self.assertIsNone(activity_bounds(signal, 0.2, 0.7))
        signal[8000:11200] = 0.02
        bounds = activity_bounds(signal, 0.2, 0.7)
        self.assertIsNotNone(bounds)
        self.assertLess(bounds[0], 0.5)
        self.assertGreater(bounds[1], 0.7)


    def test_same_call_repair_stays_together_as_two_addressed_calls_split(self):
        cues = group_fragments([
            (0, 2, "Radio check complete."),
            (2, 3.4, "Survey 24, maintain 170 until ALPHA."),
            (3.5, 4.1, "Correction, 170 until BRAVO."),
            (4.1, 5.8, "170 to BRAVO."),
        ])
        self.assertEqual(cues, [
            (0, 2, "Radio check complete."),
            (2, 4.1, "Survey 24, maintain 170 until ALPHA. Correction, 170 until BRAVO."),
            (4.1, 5.8, "170 to BRAVO."),
        ])


if __name__ == "__main__":
    unittest.main()
