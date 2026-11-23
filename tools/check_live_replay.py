"""Check live sandbox replay, evidence, stream resume, and worker recovery on a disposable database."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from replay_live import FIXTURE, ROOT, deliver, entries, payload, token, webhook_url
from smoke_demo import DATABASE_SETTINGS, http_json, local_server


def expect(condition, message):
    if not condition:
        raise RuntimeError(message)


def expect_error(code, operation, *args):
    try:
        operation(*args)
    except HTTPError as error:
        error.close()
        if error.code == code:
            return
        raise
    raise RuntimeError(f"Expected HTTP {code}.")


def frame(response):
    event_id, event = None, None
    for raw in response:
        line = raw.decode().rstrip("\r\n")
        if line.startswith("id: "):
            event_id = int(line[4:])
        elif line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            return event_id, event, json.loads(line[6:])
    raise RuntimeError("The live feed closed.")


def run_worker():
    result = subprocess.check_output([sys.executable, "-m", "review_app", "classify-live", "--once"],
                                     cwd=ROOT, timeout=15)
    return json.loads(result)["classified"]


def fail_before_commit():
    process = subprocess.run([sys.executable, "-c", """
import os
from review_app.live_chat import classify_batch, connect_editor

with connect_editor(autocommit=True) as conn, conn.transaction():
    if classify_batch(conn) == 0:
        raise RuntimeError("No pending batch to interrupt.")
    os._exit(73)
"""], cwd=ROOT, timeout=15)
    expect(process.returncode == 73, "The test worker did not reach the deliberate pre-commit failure.")


def inbox_count(exercise_id):
    from review_app.storage import connect_read

    with connect_read() as conn:
        return conn.execute(
            "SELECT count(*) AS count FROM live_chat_version WHERE exercise_id = %s AND change_seq IS NULL",
            (exercise_id,),
        ).fetchone()["count"]


def check_publication(base, review_id, source_map, expected_id):
    from evaluate_drill import http
    from review_app.review_store import get_review

    prefix = f"/exercises/{review_id}/review"
    version = http(base, prefix + "/extract", {})["version"]
    events = {item["body"]: item for item in get_review(review_id)["events"]}
    original = events["Bay 3 is OOS."]
    correction = events["Correction, Bay 2 is OOS."]
    for event, target in ((original, None), (correction, original["id"])):
        version = http(base, f"{prefix}/events/{event['id']}", {
            "version": version, "subject_id": event["subject_id"], "reported_status": event["reported_status"],
            "state": "accepted", "target_id": target,
        })["version"]
    revision = http(base, prefix + "/publish", {"version": version})["number"]
    pinned = f"/v1/exercises/{review_id}/revisions/{revision}"
    events_page = http_json(base, pinned + "/events", {"event_types": "correction"})
    item, = events_page["items"]
    expect(item["relationships"][0]["type"] == "corrects", "The published snapshot lost the correction link.")
    evidence = http_json(base, f"{pinned}/evidence/{item['evidence_ids'][0]}", {})
    expected = source_map[evidence["locator"]["line_start"] - 1]
    expect(expected["message_id"] == "bay-correction" and expected["original_path"].endswith(f"/{expected_id}/original"),
           "The snapshot line did not map back to the acknowledged original correction.")
    with urlopen(base + expected["original_path"], timeout=5) as response:
        expect(json.load(response)["text"] == evidence["excerpt"], "The snapshot and live original disagree.")


def check(folder: Path):
    os.environ["EXERCISE_REVIEW_ARCHIVE"] = str(folder / "archive")
    os.environ["ROXI_SANDBOX_TOKEN"] = "replay-fixture-local-only"
    sys.path.insert(0, str(ROOT))

    from review_app.importer import import_exercise
    from review_app.live_chat import LiveError, register_room, snapshot
    from review_app.live_export import export_snapshot
    from review_app.storage import migrate

    gold = json.loads((FIXTURE / "answer-key.json").read_text(encoding="utf-8"))
    exercise, room = gold["exercise_id"], gold["room"]
    migrate()
    try:
        snapshot(exercise)
    except LiveError as error:
        if error.status != 404:
            raise
    else:
        raise RuntimeError("Use a fresh disposable database for the replay check.")
    import_exercise(FIXTURE / "input" / "exercise.json")
    register_room(exercise, room)

    deliveries = entries(FIXTURE / "replay.ndjson")
    expect(len(deliveries) == gold["deliveries"], "Replay and answer key disagree on delivery count.")
    acknowledged, originals = {}, {}
    classifications = []
    with local_server() as base:
        url = webhook_url(base, exercise, room)
        feed = f"{base}/exercises/{exercise}/live/feed"
        secret = token()
        first = deliveries[0]["message"]
        expect_error(401, deliver, url, "", payload(first))
        expect_error(404, deliver, webhook_url(base, exercise, "wrong-room"), secret, payload(first))

        for index, entry in enumerate(deliveries, 1):
            message = entry["message"]
            key = message["message_id"], message["version"]
            body = json.dumps(message, indent=2).encode() if index == 1 else payload(message)
            code, receipt = deliver(url, secret, body)
            duplicate = index in gold["duplicates_at"]
            expect((code, receipt["duplicate"]) == (200 if duplicate else 202, duplicate),
                   f"Wrong receipt for delivery {index}.")
            if duplicate:
                expect(acknowledged[key] == receipt["id"], f"Retry changed the evidence ID for {key}.")
            else:
                acknowledged[key] = receipt["id"]
                originals[receipt["id"]] = body
            if index == gold["restart_after_delivery"]:
                expect(run_worker() == len(acknowledged), "The first worker did not classify the first batch.")
                with urlopen(feed, timeout=5) as response:
                    cursor, event, classifications = frame(response)
                expect(event == "snapshot" and cursor == len(acknowledged), "Incorrect initial snapshot.")

        expect_error(409, deliver, url, secret, payload({**first, "text": "Different body."}))
        expect_error(409, deliver, url, secret, payload({**first, "version": 2, "sender": "yard"}))
        pending = len(acknowledged) - cursor
        expect(inbox_count(exercise) == pending, "Some acknowledged messages did not survive a stopped worker.")
        fail_before_commit()
        expect(inbox_count(exercise) == pending and snapshot(exercise)[0] == cursor,
               "A worker interrupted before commit advanced the queue or lost messages.")
        expect(run_worker() == pending and run_worker() == 0, "The restarted worker did not drain exactly the pending work.")

        with urlopen(Request(feed, headers={"Last-Event-ID": str(cursor)}), timeout=5) as response:
            latest, event, changes = frame(response)
        expect(event == "updates" and latest == len(acknowledged)
               and [item["change_seq"] for item in changes] == list(range(cursor + 1, latest + 1)),
               "Reconnecting missed or replayed a committed classification.")
        classifications.extend(changes)
        by_version = {(item["message_id"], item["version"]): item for item in classifications}
        expect(len(by_version) == len(gold["versions"]) == len(classifications), "Wrong version count.")
        for expected in gold["versions"]:
            found = by_version[(expected["message_id"], expected["version"])]
            expect(all(found[key] == value for key, value in expected.items()), f"Classification mismatch: {expected}")
            address = f"{base}/exercises/{exercise}/live/evidence/{found['id']}/original"
            with urlopen(address, timeout=5) as original:
                expect(original.read() == originals[found["id"]], "The original message bytes changed.")
        _, current = snapshot(exercise)
        expect([f"{item['message_id']}@{item['version']}" for item in current] == gold["current_in_message_time_order"],
               "A late message was misplaced or an older version replaced an edit.")

        staged = folder / "review"
        expect(export_snapshot(exercise, "live-review-ci", staged) == len(current), "Incomplete freeze for review.")
        report = import_exercise(staged / "exercise.json")
        expect(report.records == len(current) and not report.issues, "The frozen snapshot did not import cleanly.")
        source_map = json.loads((staged / "source-map.json").read_text(encoding="utf-8"))
        check_publication(base, "live-review-ci", source_map, acknowledged["bay-correction", 1])
    return {"deliveries": len(deliveries), "duplicates": len(gold["duplicates_at"]), "classified_versions": len(by_version),
            "current_messages": len(current), "worker_restart_and_rollback": True, "reconnect_resume": True,
            "original_bytes": True, "review_snapshot_published": True}


if __name__ == "__main__":
    if any(not os.environ.get(name) for name in DATABASE_SETTINGS):
        raise SystemExit("Set the three database URLs to a fresh disposable PostgreSQL database.")
    with TemporaryDirectory(prefix="roxi-live-check-") as directory:
        print(json.dumps(check(Path(directory)), indent=2))
