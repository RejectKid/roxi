"""Replay concurrent civilian channels against actual PostgreSQL and loopback HTTP."""

import argparse
import asyncio
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
from time import perf_counter

from mcp import Client

from check_pilot_operations import http, mcp_server, result
from smoke_demo import DATABASE_SETTINGS, ROOT, local_server

EXERCISE = "live-context-ci"
FIXTURE = ROOT / "fixtures" / "live-context"
HISTORY = ROOT / "fixtures" / "history"
WORKSPACE = "live-context"
REVIEW = "live-context-review"


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
            ("bay-1", "Bay 1"), ("bay-2", "Bay 2"), ("bay-3", "Bay 3"), ("returns-cage-1", "Returns Cage 1"),
        )
    )
    next(entry for entry in glossary["subjects"] if entry["subject_id"] == "bay-1")["aliases"] = ["Bay one"]
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
    registry["aliases"].extend(
        {"source": f"catalog:{source}", "external_id": "dock-runner", "entities": ["west-annex-van"]}
        for source in ("yard-radio", "receiving")
    )
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


def post_json(base, path, credential, data, expected=200):
    return result(base, path, token=credential, headers={"Content-Type": "application/json"},
                  data=encoded(data), expected=expected)


def fixture_transcript(event):
    transcript = f"WEBVTT\n\nF1\n00:00:00.000 --> 00:00:01.800\n{event['text']}\n"
    if "second_cue" in event:
        transcript += f"\nF2\n00:00:02.200 --> 00:00:04.000\n{event['second_cue']}\n"
    return transcript


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
    vehicle = key["vehicle_identity"]
    linked = [row for fact in snapshot(now, entity_id=vehicle["entity_id"])["facts"] for row in fact["records"]]
    assert {row["source"].split(":", 1)[1] for row in linked} == set(vehicle["sources"])
    assert {row["medium"] for row in linked} == {None, "radio", "chat"}
    assert any(row["position"] and row["telemetry_status"] and row["reported_status"] is None for row in linked)

    citations = [cite for fact in final["facts"] for cite in fact["citations"]]
    opened = []
    for citation in citations:
        params = {field: citation[field] for field in ("exercise_id", "known_at", "as_of", "key")}
        evidence = result(base, "/v1/history/evidence", token=reader, params=params)
        assert hashlib.sha256(http(base, evidence["original_url"], token=reader)).hexdigest() == citation["sha256"]
        if citation["locator"]["kind"] == "audio_interval":
            locator = evidence["locator"]
            interval = "radio_interval_ms" if locator["value"] == key["correction"]["original"]["cue"] else "second_radio_interval_ms"
            assert [locator["start_ms"], locator["end_ms"]] == key[interval]
            assert evidence["original_record"]["generated_transcript"] == records[citation["key"]]["text"]
            opened.append(citation)
    assert {cite["sha256"] for cite in opened} == {hashlib.sha256(clip).hexdigest() for clip in radio}
    return final, len(citations)


async def agent_access(base, snapshot):
    from review_app import pilot_admin as admin
    from review_app.history_models import EvidenceRequest, HistoryAnswerRequest
    from roxi_client import RoxiClient

    user = "context.agent"
    admin.set_password(user, secrets.token_urlsafe(24))
    admin.member(WORKSPACE, user, "reader")
    for source in ("catalog:yard-radio", "catalog:receiving", "history:registry"):
        admin.grant_source(WORKSPACE, user, EXERCISE, source)
    token = admin.issue_token(user)
    pin = {field: snapshot[field] for field in ("exercise_id", "as_of", "known_at")}
    data = {**pin, "question": "Which reports disagree?"}
    answer = post_json(base, "/v1/history/answer", token, data)
    sdk = RoxiClient(base, token=token)
    assert answer == sdk.answer(HistoryAnswerRequest(**data)).model_dump(mode="json")
    async with Client(mcp_server(token), read_timeout_seconds=10) as client:
        assert answer == (await client.call_tool("answer_history", data)).structured_content
        conflict = answer["result"]["facts"][0]
        assert {cite["source"] for cite in conflict["citations"]} == {"catalog:yard-radio", "catalog:receiving"}
        citation = next(cite for cite in conflict["citations"] if cite["locator"]["kind"] == "audio_interval")
        args = {**pin, "key": citation["key"]}
        evidence = result(base, "/v1/history/evidence", token=token, params=args)
        assert evidence == (await client.call_tool("get_history_evidence", args)).structured_content
        assert evidence == sdk.evidence(EvidenceRequest(**args)).model_dump(mode="json")
        result(base, "/v1/history/changes", token=token, params={"exercise_id": EXERCISE}, expected=404)
        admin.revoke_source(WORKSPACE, user, EXERCISE, "catalog:yard-radio")
        http(base, citation["original_url"], token=token, expected=404)
        result(base, "/v1/history/evidence", token=token, params=args, expected=404)
        assert (await client.call_tool("get_history_evidence", args)).is_error
        assert not post_json(base, "/v1/history/answer", token, data)["result"]["facts"]
        assert not (await client.call_tool("answer_history", data)).structured_content["result"]["facts"]


def check_publication(base, reviewer, radio):
    from review_app import pilot_admin as admin
    from review_app.chat_types import ChatRequest
    from review_app.query_models import ExerciseRequest
    from review_app.review_store import get_review
    from roxi_client import RoxiClient

    admin.attach(WORKSPACE, REVIEW)
    app = f"/exercises/{REVIEW}"
    api = f"/v1/exercises/{REVIEW}"
    post_json(base, app + "/review/extract", reviewer, {})
    review = get_review(REVIEW)
    version = review["draft"]["version"]
    targets = {}
    for row in sorted(review["events"], key=lambda row: row["capture_offset_ms"]):
        target = targets.get("bay-3") if row["kind"] == "correction" else (
            targets.get("bay-2") if row["kind"] == "acknowledgment" else None
        )
        version = post_json(base, app + f"/review/events/{row['id']}", reviewer, {
            "version": version, "state": "accepted", "subject_id": row["subject_id"],
            "reported_status": row["reported_status"], "target_id": target,
        })["version"]
        targets[row["subject_id"]] = row["id"]
    assert post_json(base, app + "/review/publish", reviewer, {"version": version})["number"] == 1
    params = {"revision_id": 1, "question": "At 02:35, which bay was reported out of service, and what changed?"}
    ui = post_json(base, app + "/assistant/ask", reviewer, params)
    published = post_json(base, api + "/answers", reviewer, params)
    sdk = RoxiClient(base, token=reviewer)
    exercise = ExerciseRequest(exercise_id=REVIEW)
    assert sdk.latest_revision(exercise).revision_id == 1
    assert ui == published == sdk.published_answer(exercise, ChatRequest(**params)).model_dump(mode="json")
    assert published["verification"]["status"] == "supported"
    assert published["claim"]["outcome"] == "supported_correction"
    assert published["claim"]["subject_id"] == "bay-2" and published["claim"]["corrected_subject_id"] == "bay-3"
    assert len(published["citations"]) == 2
    for citation in published["citations"]:
        original = http(base, citation["original_path"], token=reviewer)
        assert hashlib.sha256(original).hexdigest() == citation["source_sha256"]
        if citation["source_kind"] == "audio":
            assert original == radio[0] and citation["locator"]["cue_id"] == "F1"
        else:
            assert b"Correction, Bay 2 is out of service." in original
    return len(published["citations"])


def speech_check(base, reader, ingestor, recording, model):
    from review_app.channel_capture import audio_url, work_radio

    stream = io.BytesIO()
    with wave.open(str(recording), "rb") as source, wave.open(stream, "wb") as clip:
        rate = source.getframerate()
        clip.setparams(source.getparams())
        source.setpos(round(22.7 * rate))
        clip.writeframes(source.readframes(round(5.5 * rate)))
    raw = stream.getvalue()
    event = {"at": "15:00:22.700", "sender": "facilities", "key": "speech-check"}
    started = perf_counter()
    capture = post(base, ingestor, "facilities-radio", envelope(event, raw))
    assert capture["state"] == "pending"
    assert work_radio(model, once=True, offline=True) == 1
    pin = {"exercise_id": EXERCISE, "known_at": datetime.now(UTC).isoformat(), "limit": 100}
    page = result(base, "/v1/history/query", token=reader, params=pin)
    url = audio_url(EXERCISE, "facilities-radio", capture["capture_id"])
    heard = [(row, cite) for fact in page["facts"] for row, cite in zip(fact["records"], fact["citations"], strict=True)
             if cite["original_url"] == url]
    text = " ".join(row["text"] for row, _ in heard)
    assert any(row["external_id"] == "bay-1" and row["reported_status"] == "in_service" for row, _ in heard), text
    assert all(row["transcription_model"] == model for row, _ in heard)
    assert http(base, url, token=reader) == raw
    return {"model": model, "upload_to_query_seconds_with_cold_model_load": round(perf_counter() - started, 3),
            "clip_seconds": 5.5, "transcript": text}


def check(folder, speech_recording=None, model="small.en"):
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
    radio = [silence(2000), silence(4200)]
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
                assert radio_batch(conn, lambda path: fixture_transcript(event) if path.read_bytes() == clip
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
        assert export_channels(EXERCISE, REVIEW, current, known_at=now)["sources"] == 4
        assert import_exercise(current / "exercise.json").records == len(events) + 2
        assert not any("pending inspection." in line for path in previous.glob("chat-*.irc")
                       for line in path.read_text().splitlines() if "Copy" in line)
        assert any("pending inspection." in line for path in current.glob("chat-*.irc")
                   for line in path.read_text().splitlines() if "Copy" in line)
        assert not any("out of service today" in path.read_text() for path in current.glob("chat-*.irc"))
        mapping = json.loads((current / "source-map.json").read_bytes())
        assert len(mapping["sources"]) == len(events) + 1 and not mapping["excluded_capture_ids"]
        snapshot, evidence_checks = history_checks(base, reader, key, pending, baseline, now, radio)
        asyncio.run(agent_access(base, snapshot))
        published_checks = check_publication(base, reader, radio)
        measured = speech_check(base, reader, ingestor, speech_recording, model) if speech_recording else None

    return {"channels": 4, "catalog_feeds": 3, "audio_byte_checks": 4, "worker_recovered": True,
            "late_edit_used_highest_version": True, "checked_history_citations": evidence_checks,
            "scoped_http_mcp_sdk_match": True, "revocation_checked": True, "checked_published_citations": published_checks,
            "exported_sources": len(mapping["sources"]), "speech_check": measured}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speech-recording", type=Path, help="Generated West Annex facilities-radio.wav")
    parser.add_argument("--model", default="small.en", help="Local cached Faster Whisper model for the speech check")
    options = parser.parse_args()
    if not all(os.environ.get(var) for var in DATABASE_SETTINGS):
        raise SystemExit("Set all three EXERCISE_REVIEW_* database URLs for a disposable test database.")
    with TemporaryDirectory(prefix="roxi-live-context-") as temporary:
        print(json.dumps(check(Path(temporary), options.speech_recording, options.model), indent=2))
