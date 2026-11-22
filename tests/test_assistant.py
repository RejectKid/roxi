import unittest
from types import SimpleNamespace
from unittest.mock import patch

from review_app.chat_types import Claim
from review_app.chatbot import plan_question, read_clock
from review_app.query_models import ChatLines, EvidenceResult, ReportEvent, ReportPayload
from review_app.transcription import split_at_silence, timestamp
from review_app.verifier import evaluate_claim

GLOSSARY = {"subjects": [{"subject_id": "bay-2", "label": "Bay 2", "aliases": ["Bay two"]}]}


class AssistantTests(unittest.TestCase):
    def test_parser_keeps_precise_cutoffs_and_cue_scope(self):
        self.assertEqual(read_clock("At 02:28.999, what was reported?"), 148999)
        with patch("review_app.chatbot.classify_locally", return_value=(None, "rules fallback")):
            plan = plan_question("Was Bay two confirmed back in service by 12:30?", GLOSSARY)
            self.assertEqual((plan.purpose, plan.subject, plan.status), ("status", "bay-2", "in_service"))
            self.assertTrue(plan.asks_confirmation)
            cue = plan_question("Which cart was mentioned in cue F18?", GLOSSARY)
            self.assertEqual((cue.purpose, cue.cue), ("source", "F18"))

    def test_transcription_does_not_join_words_across_a_silent_gap(self):
        segment = SimpleNamespace(words=[
            SimpleNamespace(start=1.0, end=1.2, word=" Bay"),
            SimpleNamespace(start=1.25, end=1.5, word=" two."),
            SimpleNamespace(start=3.0, end=3.6, word=" Copy."),
        ])
        self.assertEqual(list(split_at_silence(segment)), [(1.0, 1.5, " Bay two."), (3.0, 3.6, " Copy.")])
        self.assertEqual(timestamp(642123), "00:10:42.123")

    def test_verifier_rejects_wrong_status_and_out_of_cutoff_evidence(self):
        evidence = EvidenceResult(
            exercise_id="drill-01", revision_id=1, evidence_id="s3-r7", source_file_id=3, channel="dispatch",
            source_kind="chat", original_name="dispatch.irc", speaker="dispatch", capture_offset_ms=149000,
            timing_quality="exact", excerpt="Bay two is out of service.", locator=ChatLines(line_start=7, line_end=7),
            original_excerpt="Bay two is out of service.", original_text="[00:02:29] <dispatch> Bay two is out of service.",
            source_sha256="0" * 64, source_byte_size=123, original_path="/v1/example", playback_path=None,
        )
        event = ReportEvent(
            event_id=6, subject_id="bay-2", capture_offset_ms=149000, timing_quality="exact",
            review_state="accepted", evidence_ids=[evidence.evidence_id], relationships=[],
            conflicting_event_ids=[], payload=ReportPayload(reported_status="out_of_service"),
        )
        claim = Claim(
            exercise_id="drill-01", revision_id=1, outcome="supported_report", as_of_ms=149000,
            subject_id="bay-2", reported_status="out_of_service", event_ids=[6], evidence_ids=[evidence.evidence_id],
        )
        args = ({evidence.evidence_id: evidence}, {}, {6: event}, {"bay-2": ["Bay 2", "Bay two"]})
        self.assertEqual(evaluate_claim(claim, *args).status, "supported")
        for change in ({"reported_status": "in_service"}, {"as_of_ms": 148999}):
            self.assertEqual(evaluate_claim(claim.model_copy(update=change), *args).status, "contradicted")
