"""Check the West Annex import, publication, and source-backed answers against a disposable database."""

import asyncio
import json
import os
import shutil
import socket
import wave
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from urllib.error import URLError

import uvicorn

from check_published_parity import ROOT, http_json, verify

EXERCISE = "drill-ci"
DATABASE_SETTINGS = ("EXERCISE_REVIEW_DATABASE_URL", "EXERCISE_REVIEW_EDITOR_URL", "EXERCISE_REVIEW_READER_URL")
REVIEWED = (
    ("F06", None),
    (7, "F06"),
    (8, 7),
    (14, None),
    (15, None),
    (16, None),
    ("F14", None),
    (20, None),
    ("U02", None),
    ("F18", None),
)


def make_fixture(folder: Path) -> Path:
    source = ROOT / "fixtures" / "drill-01" / "input"
    shutil.copytree(source, folder, ignore=shutil.ignore_patterns("*.wav"))
    manifest = folder / "exercise.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["exercise_id"] = EXERCISE
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    # Deterministic PCM exercises archive, hash, and byte-range behavior without a voice model.
    for seed, (filename, seconds) in enumerate((("facilities-radio.wav", 900), ("unaligned-radio.wav", 45))):
        generator = Random(seed)
        with wave.open(str(folder / filename), "wb") as recording:
            recording.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
            for _ in range(seconds):
                recording.writeframesraw(generator.randbytes(32_000))
    return manifest


def review_and_publish(base: str) -> int:
    from evaluate_drill import http
    from review_app.review_store import get_review

    prefix = f"/exercises/{EXERCISE}/review"
    version = http(base, prefix + "/extract", {})["version"]
    by_locator = {event["cue_id"] or event["ordinal"]: event
                  for event in get_review(EXERCISE)["events"]}
    for locator, link in REVIEWED:
        event = by_locator[locator]
        version = http(base, f"{prefix}/events/{event['id']}", {
            "version": version,
            "subject_id": event["subject_id"],
            "reported_status": event["reported_status"],
            "state": "accepted",
            "target_id": by_locator[link]["id"] if link else None,
        })["version"]
    publication = http(base, prefix + "/publish", {"version": version})
    if publication["number"] != 1 or http(base, prefix + "/publish", {"version": version}) != publication:
        raise RuntimeError("An identical publication retry should return the same first revision.")
    return publication["number"]


def wait_ready(base: str, thread: Thread) -> None:
    deadline = monotonic() + 15
    while thread.is_alive() and monotonic() < deadline:
        try:
            if http_json(base, "/healthz", {})["status"] == "ok":
                return
        except URLError:
            sleep(0.1)
    raise RuntimeError("The local smoke-test server did not become healthy.")


def run(folder: Path) -> None:
    os.environ["EXERCISE_REVIEW_ARCHIVE"] = str(folder / "archive")
    os.environ["EXERCISE_REVIEW_API_EXERCISES"] = EXERCISE
    os.environ["EXERCISE_REVIEW_CHAT_MODEL"] = ""

    from evaluate_drill import chat_passed, score_chat
    from review_app.importer import import_exercise
    from review_app.review_store import ReviewError, get_review
    from review_app.storage import migrate

    migrate()
    try:
        get_review(EXERCISE)
    except ReviewError as error:
        if error.status != 404:
            raise
    else:
        raise RuntimeError("Use a fresh disposable database. The smoke exercise already exists.")

    manifest = make_fixture(folder / "input")
    result = import_exercise(manifest)
    if (result.new_source_versions, result.records, result.issues) != (3, 62, 1):
        raise RuntimeError(f"Unexpected import result: {result}")
    if import_exercise(manifest).new_source_versions != 0:
        raise RuntimeError("An identical import should create no new source versions.")

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config("review_app.app:app", log_level="warning"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            wait_ready(base, thread)
            revision = review_and_publish(base)
            asyncio.run(verify(base, EXERCISE))
            report = score_chat(base, EXERCISE, revision, source_root=manifest.parent)
            print(json.dumps(report, indent=2, ensure_ascii=True))
            if not chat_passed(report):
                raise RuntimeError("The assistant or its evidence did not match the drill key.")
        finally:
            server.should_exit = True
            thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("The smoke-test server did not stop.")


if __name__ == "__main__":
    if any(not os.environ.get(name) for name in DATABASE_SETTINGS):
        raise SystemExit("Set the three database URLs to a fresh disposable PostgreSQL database.")
    with TemporaryDirectory(prefix="roxi-smoke-") as directory:
        run(Path(directory))
