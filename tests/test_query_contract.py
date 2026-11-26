import unittest

from review_app import query_models as model
from review_app import query_service as service
from review_app.app import app
from review_app.mcp_server import TOOLS


class PublishedQueryTests(unittest.TestCase):
    def test_cursor_is_bound_to_revision_filters_and_query(self):
        request = model.RecordRequest(exercise_id="drill-01", revision_id=2, channel="dispatch")
        context = service.filter_fingerprint("records", request)
        cursor = service.encode_cursor(context, (False, 340000, 3, 14))
        self.assertEqual(service.decode_cursor(cursor, context), (False, 340000, 3, 14))
        for changed in (
            request.model_copy(update={"revision_id": 1}),
            request.model_copy(update={"channel": "facilities"}),
        ):
            with self.assertRaises(service.QueryError):
                service.decode_cursor(cursor, service.filter_fingerprint("records", changed))
        with self.assertRaises(service.QueryError):
            service.decode_cursor(cursor, service.filter_fingerprint("events", request))
        with self.assertRaises(service.QueryError):
            service.decode_cursor("invalid!", context)

    def test_evidence_id_pins_a_transcript_without_changing_the_raw_source(self):
        original = service.evidence_id(1, 18, "audio", "Cart C-17.")
        edited = service.evidence_id(1, 18, "audio", "Cart [unclear].")
        self.assertNotEqual(original, edited)
        self.assertEqual(original.rsplit("-t", 1)[0], edited.rsplit("-t", 1)[0])
        self.assertEqual(service.evidence_id(3, 7, "chat", "Correction."), "s3-r7")

    def test_links_respect_cutoff_and_review_state(self):
        original = {"id": 5, "kind": "status_report", "state": "accepted", "subject_id": "bay-3",
                    "capture_offset_ms": 124000, "source_file_id": 1, "ordinal": 6, "source_kind": "audio",
                    "body": "Bay three is OOS.", "target_id": None}
        correction = {"id": 6, "kind": "correction", "state": "accepted", "subject_id": "bay-2",
                      "capture_offset_ms": 149000, "source_file_id": 3, "ordinal": 7, "source_kind": "chat",
                      "body": "Correction, Bay 2 is OOS.", "target_id": 5}
        self.assertEqual(service.published_links([original, correction], 148999, "accepted"), {})
        edges = service.published_links([original, correction], 149000, "accepted")
        self.assertEqual((edges[5][0].direction, edges[5][0].subject_id), ("incoming", "bay-2"))
        self.assertEqual((edges[6][0].direction, edges[6][0].event_id), ("outgoing", 5))
        correction["state"] = "proposed"
        self.assertEqual(service.published_links([original, correction], 149000, "accepted"), {})

    def test_http_schema_and_mcp_share_the_same_result_types(self):
        operations = {
            operation["operationId"]: operation for endpoint in app.openapi()["paths"].values()
            for method, operation in endpoint.items()
            if method in {"get", "post"} and operation["operationId"] in TOOLS
        }
        self.assertEqual(set(operations), set(TOOLS))
        for name, operation in operations.items():
            schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
            self.assertEqual(schema["$ref"].rsplit("/", 1)[-1], TOOLS[name].output.__name__)
