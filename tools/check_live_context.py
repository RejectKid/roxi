"""Replay concurrent civilian channels against actual PostgreSQL and loopback HTTP."""

import base64
import hashlib
import io
import json
import os
import secrets
import shutil
import sys
import wave
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from check_pilot_operations import http, result
from smoke_demo import DATABASE_SETTINGS, ROOT, local_server

EXERCISE = "live-context-ci"
FIXTURE = ROOT / "fixtures" / "live-context"
HISTORY = ROOT / "fixtures" / "history"
WORKSPACE = "live-context"


def encoded(value):
    return json.dumps(value).encode("utf-8")


def silence(length_ms):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as clip:
        clip.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        clip.writeframes(bytes(length_ms * 16))
    return stream.getvalue()


def input_bundle(folder, events):
    from review_app.history_registry import load_registry
    from review_app.importer import import_exercise
    from review_app.source_catalog import ingest_file, register
    from review_app.storage import migrate

    migrate()
    source_input = folder / "exercise"
    shutil.copytree(HISTORY / "input", source_input)
    manifest = json.loads((source_input / "exercise.json").read_bytes())
    manifest.update(exercise_id=EXERCISE, name="Fictional multi-channel logistics replay")
    (source_input / "exercise.json").write_bytes(encoded(manifest))
    glossary = json.loads((source_input / "glossary.json").read_bytes())
    glossary["subjects"].extend(
        {"subject_id": key, "label": label} for key, label in (
            ("bay-2", "Bay 2"), ("bay-3", "Bay 3"), ("returns-cage-1", "Returns Cage 1"),
        )
    )
    (source_input / "glossary.json").write_bytes(encoded(glossary))
    import_exercise(source_input / "exercise.json")
    catalog = folder / "catalog.json"
    catalog.write_bytes(encoded(events["catalog"] + json.loads((HISTORY / "catalog.json").read_bytes())[:3]))
    assert register(EXERCISE, catalog) == 7
    for key, file in (("assets", "assets.csv"), ("sites", "sites.csv"), ("fleet", "fleet-on-time.json")):
        assert ingest_file(EXERCISE, key, HISTORY / file).state == "accepted"
    registry = json.loads((HISTORY / "registry-v1.json").read_bytes())
    registry["entities"].append({"id": "returns-cage", "label": "Returns Cage 1", "kind": "site"})
    registry["aliases"].extend(
        {"source": f"catalog:{source}", "external_id": "returns-cage-1", "entities": ["returns-cage"]}
        for source in ("yard-radio", "receiving")
    )
    registry["aliases"].append({"source": "catalog:receiving", "external_id": "yard-runner",
                                "entities": ["west-annex-van", "yard-cart"]})
    registry_path = folder / "registry.json"
    registry_path.write_bytes(encoded(registry))
    load_registry(EXERCISE, registry_path)


def credentials():
    from review_app import pilot_admin as admin

    admin.workspace(WORKSPACE, "Multi-channel fixture")
    admin.attach(WORKSPACE, EXERCISE)
    admin.set_password("context.fixture", secrets.token_urlsafe(24))
    admin.member(WORKSPACE, "context.fixture", "reviewer", True)
    admin.set_password("context.ingest", secrets.token_urlsafe(24))
    admin.member(WORKSPACE, "context.ingest", "ingest")
    for source in ("facilities-radio", "yard-radio", "dispatch", "receiving"):
        admin.grant_source(WORKSPACE, "context.ingest", EXERCISE, f"catalog:{source}")
    return admin.issue_token("context.fixture"), admin.issue_token("context.ingest")


def envelope(event, clip=None, version=1, text=None):
    data = {"observed_at": "2026-11-22T" + event["at"] + "Z", "sender": event["sender"]}
    if clip is not None:
        return {**data, "schema": "roxi.completed-radio/v1", "clip_id": event["key"],
                "wav_base64": base64.b64encode(clip).decode("ascii")}
    return {**data, "schema": "roxi.channel-chat/v1", "message_id": event["key"],
            "version": version, "text": text or event["text"]}


def post(base, ingestor, source, data, expected=202):
    return result(base, f"/v1/exercises/{EXERCISE}/channels/{source}/captures", token=ingestor,
                  headers={"Content-Type": "application/json"}, data=encoded(data), expected=expected)


def fixture_transcript(text):
    return f"WEBVTT\n\nF1\n00:00:00.000 --> 00:00:01.800\n{text}\n"


def history_checks(base, reader, key, pending, baseline, now, radio):
    def pin(at):
        return {"exercise_id": EXERCISE, "known_at": at.isoformat(), "as_of": "2026-11-22T15:30:00Z"}

    def snapshot(at, **filters):
        return result(base, "/v1/history/query", token=reader, params={**pin(at), **filters})

    before = snapshot(pending)
    assert sum("awaiting transcription" in gap for gap in before["gaps"]) == key["pending_audio"]
    final = snapshot(now)
    assert any(key["outage_source"] in gap and "unavailable" in gap for gap in final["gaps"])
    records = {record["key"]: record for fact in final["facts"] for record in fact["records"]}
    assert {record["source"] for record in records.values()} == {
        "catalog:" + source for source in ("assets", "sites", "fleet", "dispatch", "receiving", "yard-radio", "facilities-radio")
    }

    correction = next(row for row in records.values() if row["kind"] == "correction")
    target = next(row for row in records.values() if row["source"] == "catalog:" + key["correction"]["original"]["source"])
    assert target["key"] in correction["candidate_target_keys"] and correction["state"] == "proposed"
    assert correction["external_id"] == "bay-2"
    ack = next(row for row in records.values() if row["kind"] == "acknowledgment")
    assert ack["input_version"] == 3 and ack["reported_status"] is None
    assert ack["sender"] == key["acknowledgment"]["sender"] and ack["candidate_target_keys"] == [correction["key"]]
    uncertain = [row for row in records.values() if row["identity_status"] == "ambiguous"]
    assert uncertain and any(row["late"] for row in uncertain)
    assert all(row["entity_id"] is None and row["candidate_entity_ids"] == key["ambiguous_alias"]["candidate_ids"]
               for row in uncertain)
    changes = snapshot(now, mode="changes", baseline_known_at=baseline.isoformat())
    edit = next(fact for fact in changes["facts"] if fact["kind"] == "updated" and fact["records"][-1]["key"] == ack["key"])
    assert [row["input_version"] for row in edit["records"]] == [1, 3]
    conflicts = snapshot(now, mode="conflicts")
    assert len(conflicts["facts"]) == 1
    conflict = conflicts["facts"][0]
    assert conflict["subject"] == key["conflict"]["subject"]
    assert {row["reported_status"] for row in conflict["records"]} == set(key["conflict"]["statuses"])

    citations = [cite for fact in final["facts"] for cite in fact["citations"]]
    opened = []
    for citation in citations:
        params = {field: citation[field] for field in ("exercise_id", "known_at", "as_of", "key")}
        evidence = result(base, "/v1/history/evidence", token=reader, params=params)
        assert hashlib.sha256(http(base, evidence["original_url"], token=reader)).hexdigest() == citation["sha256"]
        if citation["locator"]["kind"] == "audio_interval":
            locator = evidence["locator"]
            assert [locator["start_ms"], locator["end_ms"]] == key["radio_interval_ms"]
            assert locator["value"] == key["correction"]["original"]["cue"]
            assert evidence["original_record"]["generated_transcript"] == records[citation["key"]]["text"]
            opened.append(citation)
    assert {cite["sha256"] for cite in opened} == {hashlib.sha256(clip).hexdigest() for clip in radio}
    return final, len(citations)


def check(folder):
    os.environ["EXERCISE_REVIEW_ARCHIVE"] = str(folder / "archive")
    sys.path.insert(0, str(ROOT))
    from review_app.channel_capture import audio_url, radio_batch, lookup
    from review_app.channel_export import export_channels
    from review_app.importer import import_exercise
    from review_app.live_chat import connect_editor
    from review_app import source_catalog as catalog
    from review_app.storage import archive_path

    replay = json.loads((FIXTURE / "replay.json").read_bytes())
    key = json.loads((FIXTURE / "answer-key.json").read_bytes())
    input_bundle(folder, replay)
    reader, ingestor = credentials()
    events = replay["events"]
    radio = [silence(2000), silence(2120)]
    with local_server(pilot=True) as base:
        receipts = [post(base, ingestor, event["source"], envelope(event, clip))
                    for event, clip in zip(events, radio + [None] * (len(events) - len(radio)), strict=True)]
        assert [row["state"] for row in receipts].count("pending") == key["pending_audio"]
        pending = datetime.now(UTC)
        idem = post(base, ingestor, events[2]["source"], envelope(events[2]), expected=200)
        assert idem["duplicate"] and idem["capture_id"] == receipts[2]["capture_id"]
        assert receipts[2]["capture_id"] != receipts[3]["capture_id"]
        post(base, ingestor, "dispatch", envelope(events[2], text="Edited in place"), expected=409)
        post(base, ingestor, "facilities-radio", envelope(events[0], b"not a WAV"), expected=422)
        post(base, ingestor, "dispatch", envelope(events[2], radio[0]), expected=422)
        post(base, reader, "dispatch", envelope(events[2]), expected=404)

        def fail(_):
            raise RuntimeError("Simulated worker interruption")

        try:
            with connect_editor() as conn:
                radio_batch(conn, fail)
        except RuntimeError as error:
            assert str(error) == "Simulated worker interruption"
        else:
            raise AssertionError("Worker interruption did not propagate.")

        for event, clip, receipt in zip(events[:2], radio, receipts[:2], strict=True):
            url = audio_url(EXERCISE, event["source"], receipt["capture_id"])
            assert result(base, url.removesuffix("/audio"), token=reader)["state"] == "pending"
            http(base, url, token=ingestor, expected=404)
            with connect_editor() as conn:
                assert radio_batch(conn, lambda path: fixture_transcript(event["text"]) if path.read_bytes() == clip
                                   else fail(path))
            assert result(base, url.removesuffix("/audio"), token=reader)["state"] == "accepted"
            assert http(base, url, token=reader) == clip
            assert http(base, url, token=reader, headers={"Range": "bytes=44-79"}, expected=206) == clip[44:80]
        with connect_editor() as conn:
            assert not radio_batch(conn, fail)

        baseline = datetime.now(UTC)
        edit = post(base, ingestor, "receiving", envelope(events[4], version=3,
                    text="Copy, Bay 2 is out of service pending inspection."))
        post(base, ingestor, "receiving", envelope(events[4], version=2, text="Copy, Bay 2 is out of service today."))
        late = {**events[5], "key": "message-4", "at": "15:02:03"}
        post(base, ingestor, "receiving", envelope(late))
        assert catalog.ingest_file(EXERCISE, "fleet", HISTORY / "fleet-late.json").state == "accepted"
        catalog.record_failure(EXERCISE, "fleet", "file", "Simulated fleet connection outage")
        now = datetime.now(UTC)

        captured = lookup(EXERCISE, "receiving", edit["capture_id"])
        assert json.loads(archive_path(captured["digest"]).read_bytes())["version"] == 3
        previous = folder / "previous"
        current = folder / "current"
        assert export_channels(EXERCISE, "live-context-review-old", previous, known_at=baseline)["sources"] == 4
        assert export_channels(EXERCISE, "live-context-review", current, known_at=now)["sources"] == 4
        assert import_exercise(current / "exercise.json").records == len(events) + 1
        assert not any("pending inspection." in line for path in previous.glob("chat-*.irc")
                       for line in path.read_text().splitlines() if "Copy" in line)
        assert any("pending inspection." in line for path in current.glob("chat-*.irc")
                   for line in path.read_text().splitlines() if "Copy" in line)
        assert not any("out of service today" in path.read_text() for path in current.glob("chat-*.irc"))
        mapping = json.loads((current / "source-map.json").read_bytes())
        assert len(mapping["sources"]) == len(events) + 1 and not mapping["excluded_capture_ids"]
        _, evidence_checks = history_checks(base, reader, key, pending, baseline, now, radio)

    return {"channels": 4, "catalog_feeds": 3, "audio_byte_checks": 4, "worker_recovered": True,
            "late_edit_used_highest_version": True, "checked_history_citations": evidence_checks,
            "exported_sources": len(mapping["sources"])}


if __name__ == "__main__":
    if not all(os.environ.get(var) for var in DATABASE_SETTINGS):
        raise SystemExit("Set all three EXERCISE_REVIEW_* database URLs for a disposable test database.")
    with TemporaryDirectory(prefix="roxi-live-context-") as temporary:
        print(json.dumps(check(Path(temporary)), indent=2))
