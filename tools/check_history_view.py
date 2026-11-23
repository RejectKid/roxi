"""Replay the fictional cross-source exercise and check both historical cutoffs and evidence."""

import json
import os
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from seed_history import EXERCISE, FIXTURE, json_file, seed
from smoke_demo import DATABASE_SETTINGS, http_json, local_server


def moment(value):
    return datetime.fromisoformat(value) if value else None


def check(folder: Path) -> dict:
    os.environ["EXERCISE_REVIEW_ARCHIVE"] = str(folder / "archive")
    os.environ["EXERCISE_REVIEW_API_EXERCISES"] = EXERCISE
    from review_app.live_chat import ChatMessage, receive, work
    from review_app.storage import connect_read, migrate

    migrate()
    receipt = seed()
    assert seed() == receipt
    expected = json_file(FIXTURE / "answer-key.json")
    with local_server() as base:
        prefix = f"/exercises/{EXERCISE}/history"

        def read(phase, **overrides):
            values = {"as_of": receipt["as_of"], "known_at": receipt["known_at"][phase], **overrides}
            return http_json(base, prefix + "/view.json", values)

        before, after, revised = (read(phase) for phase in ("before_late", "after_late", "after_revisions"))
        assert [len(before["observations"]), len(after["observations"])] == [expected["before_count"], expected["after_count"]]
        assert [value["registry"]["version"] for value in (before, after, revised)] == expected["registry_versions"]
        assert [value["layers"][0]["version"] for value in (before, after, revised)] == expected["layer_versions"]
        effective = read("after_revisions", as_of="2026-11-22T15:01:00Z")
        assert effective["layers"][0]["version"] == 1
        assert not any(item["late"] for item in before["observations"])
        late, = (item for item in after["observations"] if item["late"])
        assert moment(late["observed_at"]) == moment(expected["late_observation_at"])
        assert moment(before["known_at"]) < moment(late["received_at"]) <= moment(after["known_at"])
        assert late["entity"]["id"] == expected["canonical_van_id"]
        assert late["evidence"]["locator"]["value"] == expected["late_locator"]
        assert late["evidence"]["parser_version"] == 1
        revised_late, = (item for item in revised["observations"] if item["late"])
        assert revised_late["evidence"]["parser_version"] == 2
        assert revised_late["evidence"]["original_url"] == late["evidence"]["original_url"]

        report, = (item for item in after["observations"]
                   if item["data"].get("message_id") == expected["conflicting_message_id"])
        assert report["entity"]["id"] == late["entity"]["id"]
        assert report["conflicts"] == [late["key"]] and late["conflicts"] == [report["key"]]
        ambiguous, = (item for item in after["observations"]
                      if item["data"].get("message_id") == expected["ambiguous_message_id"])
        assert ambiguous["entity"] is None and ambiguous["identity"] == "ambiguous"
        assert [item["id"] for item in ambiguous["candidates"]] == expected["alias_candidates"]
        positions = [item for item in after["observations"] if item["position"]]
        assert {"site", "vehicle_observation", "weather_observation"} <= {item["kind"] for item in positions}
        asset, = (item for item in after["observations"] if item["kind"] == "asset")
        assert asset["entity"]["id"] == report["entity"]["id"]
        assert read("before_late") == before

        for url, filename in (
            (late["evidence"]["original_url"], "fleet-late.json"),
            (before["registry"]["original_url"], "registry-v1.json"),
            (before["layers"][0]["original_url"], "layout-v1.geojson"),
            (revised["layers"][0]["original_url"], "layout-v2.geojson"),
        ):
            with urlopen(base + url, timeout=5) as response:
                assert response.read() == (FIXTURE / filename).read_bytes()
        with urlopen(base + report["evidence"]["original_url"], timeout=5) as response:
            assert json.load(response)["message_id"] == expected["conflicting_message_id"]
        with urlopen(base + receipt["views"]["after_late"], timeout=5) as response:
            rendered = response.read()
            assert all(value in rendered for value in (b"Facility map", b"Late arrival", b"Conflicting status",
                                                        b"Unresolved alias", b"Reviewed aliases"))

        edit = {**json_file(FIXTURE / "chat.json")[0], "version": 2, "text": "North Shuttle is in service."}
        edit_body = json.dumps(edit).encode()
        item_id = receive(EXERCISE, "receiving-dock", ChatMessage.model_validate(edit), edit_body)["id"]
        assert work(once=True) == 1
        with connect_read() as conn:
            timing = conn.execute("SELECT received_at, classified_at FROM live_chat_version WHERE id = %s",
                                  (item_id,)).fetchone()
        unfinished = read("after_revisions", known_at=timing["received_at"].isoformat())
        finished = read("after_revisions", known_at=timing["classified_at"].isoformat())
        before_edit = next(item for item in unfinished["observations"] if item["data"].get("message_id") == edit["message_id"])
        after_edit = next(item for item in finished["observations"] if item["data"].get("message_id") == edit["message_id"])
        assert before_edit["data"]["version"] == 1 and before_edit["conflicts"]
        assert after_edit["data"]["version"] == 2 and not after_edit["conflicts"]
        assert moment(after_edit["observed_at"]) == moment(before_edit["observed_at"])
        assert read("after_late") == after

    return {"earlier_view": len(before["observations"]), "later_view": len(after["observations"]),
            "late_observation": expected["late_observation_at"], "same_entity_across_sources": True,
            "ambiguous_identity_preserved": True, "both_conflicting_statuses": True,
            "historical_registry_layer_and_parser_pins": True, "chat_edit_pinned_by_processing_time": True,
            "original_evidence_verified": True}


if __name__ == "__main__":
    if any(not os.environ.get(name) for name in DATABASE_SETTINGS):
        raise SystemExit("Set the three database URLs to a fresh disposable PostgreSQL database.")
    with TemporaryDirectory(prefix="roxi-history-check-") as directory:
        print(json.dumps(check(Path(directory)), indent=2))
