import json
import unittest
from dataclasses import asdict
from pathlib import Path

from review_app.extraction import Extractor, describe_review, radio_readback
from review_app.importer import parse_chat, parse_vtt

INPUT = Path(__file__).resolve().parents[1] / "fixtures" / "drill-01" / "input"


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.extractor = Extractor(json.loads((INPUT / "glossary.json").read_text(encoding="utf-8")))

    def test_fixture_links_conflicts_and_unknown_clock(self):
        facilities = parse_vtt((INPUT / "facilities-radio.vtt").read_text(encoding="utf-8"), 900_000, 0)
        secondary = parse_vtt((INPUT / "unaligned-radio.vtt").read_text(encoding="utf-8"), 45_000, None)
        chat, _ = parse_chat((INPUT / "dispatch.irc").read_text(encoding="utf-8"))
        events = []
        for source_id, records in enumerate((facilities, secondary, chat), start=1):
            for record in records:
                proposal = self.extractor.extract(record.body)
                if proposal:
                    events.append(
                        {**asdict(proposal), "id": len(events) + 1, "source_file_id": source_id,
                         "ordinal": record.ordinal, "body": record.body, "capture_offset_ms": record.capture_ms,
                         "state": "proposed", "target_id": None}
                    )
        describe_review(events)
        at = {(event["source_file_id"], event["ordinal"]): event for event in events}
        original, correction, ack = (at[key] for key in ((1, 6), (3, 7), (3, 8)))
        self.assertEqual(original["subject_id"], "bay-3")
        self.assertEqual(correction["subject_id"], "bay-2")
        self.assertEqual(correction["candidates"], [original["id"]])
        self.assertEqual(ack["candidates"], [correction["id"]])
        self.assertCountEqual(at[3, 16]["candidates"], [at[3, 14]["id"], at[3, 15]["id"]])
        self.assertEqual(at[1, 13]["candidates"], [])
        self.assertEqual(at[1, 14]["conflicts"], [at[3, 20]["id"]])
        self.assertIsNone(at[2, 2]["capture_offset_ms"])
        self.assertEqual(at[2, 2]["subject_id"], "bay-2")

    def test_location_does_not_replace_an_unresolved_subject(self):
        self.assertIsNone(self.extractor.extract("Cart [unclear] is staged beside Bay one.").subject_id)
        self.assertEqual(self.extractor.extract("Cart C-17 is staged beside Bay one.").subject_id, "cart-c17")

    def test_negation_and_shared_alias(self):
        self.assertEqual(self.extractor.extract("Bay one is not in service.").reported_status, "out_of_service")
        extractor = Extractor({"subjects": [{"subject_id": "first", "label": "Cage 1", "aliases": ["Cage"]},
                                            {"subject_id": "second", "label": "Cage 2", "aliases": ["Cage"]}]})
        self.assertIsNone(extractor.extract("Cage is out of service.").subject_id)


    def test_inline_radio_correction_and_unannounced_readback_link_only_the_matching_call(self):
        call = {"source_file_id": 1, "ordinal": 2, "speaker": "Dispatch",
                "audio_start_ms": 1000, "audio_end_ms": 4000, "capture_offset_ms": 1000}
        response = {**call, "ordinal": 3, "speaker": "Driver", "audio_start_ms": 4010, "capture_offset_ms": 4010}
        body = "Maintain one seven zero knots to ALPHA. Correction, one hundred seventy knots until BRAVO."
        original, correction = self.extractor.extract_radio(body)
        acknowledgment = radio_readback("One seventy to BRAVO.", response, call, correction)
        events = [
            {**vars(item), **record, "id": index, "body": text, "state": "proposed", "target_id": None, "clause": clause}
            for index, (item, record, text, clause) in enumerate([
                (original, call, body, -1), (correction, call, body, 0),
                (acknowledgment, response, "One seventy to BRAVO.", 0),
            ], 1)
        ]
        describe_review(events)
        self.assertEqual([event["candidates"] for event in events], [[], [1], [2]])
        self.assertEqual(original.radio_speed_limit.until, "ALPHA")
        self.assertEqual(correction.radio_speed_limit.until, "BRAVO")
        self.assertIsNone(radio_readback("One seventy to ALPHA.", response, call, correction))
        self.assertIsNone(radio_readback("One seventy to BRAVO.", {**response, "source_file_id": 2}, call, correction))
        self.assertIsNone(radio_readback("One seventy to BRAVO.", {**response, "speaker": "Dispatch"}, call, correction))
        self.assertIsNone(radio_readback("One seventy to BRAVO.", {**response, "ordinal": 4}, call, correction))
        self.assertIsNone(self.extractor.extract_radio("Maintain 170 until BRAVO. Correction, 170 until BRAVO."))
        self.assertIsNone(self.extractor.extract_radio("Maintain one one twenty to ALPHA. Correction 170 until BRAVO."))
