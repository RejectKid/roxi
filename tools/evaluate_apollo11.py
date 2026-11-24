"""Score Roxi on the archived Apollo 11 Day 1 timing correction in a new test database."""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import wave
from dataclasses import dataclass
from html import escape, unescape
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urldefrag
from urllib.request import urlopen

import av

from check_pilot_operations import result
from evaluate_drill import check_audio_bytes, edit_distance, source_hash, words
from smoke_demo import DATABASE_SETTINGS, local_server

AUDIO = "https://www.nasa.gov/wp-content/uploads/2015/03/Apollo-11_Day-01-Highlights.mp3"
TRANSCRIPT = "https://www.nasa.gov/wp-content/uploads/static/history/alsj/a11/a11transcript_tec.html"
AUDIO_SHA256 = "f6ec3f8b7cb9d7615036c0663b1bac2b4eec9f9762e94b5f109dd3d3edc376d8"
TRANSCRIPT_SHA256 = "daf141a2c9030b0ca1e8748962af7a4cbfe227ed324f5c5ca085f063db271b3a"
START_MS, END_MS = 332_000, 383_000
SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class Turn:
    name: str
    mission: str
    start_ms: int
    end_ms: int
    kind: str


# Milliseconds within the highlights excerpt. Mission time labels refer to tape 2/9, page 17.
TURNS = (
    Turn("original-report", "00 02 54 09", 1420, 19230, "time_report"),
    Turn("crew-readback", "00 02 54 33", 25420, 37940, "acknowledgment"),
    Turn("correction", "00 02 54 46", 38510, 43080, "correction"),
    Turn("crew-acknowledgment", "00 02 54 55", 47750, 48260, "acknowledgment"),
)
LINKS = (
    ("crew-readback", "original-report"),
    ("correction", "original-report"),
    ("crew-acknowledgment", "correction"),
)
EXPECTED_TIMES = {"original-report": 11700, "correction": 11703}


def source(url: str, folder: Path, digest: str) -> Path:
    path = folder / url.rsplit("/", 1)[1]
    if not path.is_file():
        with NamedTemporaryFile(dir=folder, suffix=".part", delete=False) as output:
            partial = Path(output.name)
            try:
                with urlopen(url, timeout=90) as response:
                    shutil.copyfileobj(response, output)
                output.close()
                if source_hash(partial) != digest:
                    raise ValueError(f"The downloaded NASA file changed: {path.name}")
                partial.replace(path)
            finally:
                output.close()
                partial.unlink(missing_ok=True)
    if source_hash(path) != digest:
        raise ValueError(f"The cached NASA file changed: {path.name}")
    return path


def excerpt(mp3: Path, destination: Path) -> None:
    with av.open(str(mp3)) as audio, wave.open(str(destination), "wb") as wav:
        wav.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        first = START_MS * SAMPLE_RATE // 1000
        last = END_MS * SAMPLE_RATE // 1000
        position = 0
        for frame in audio.decode(audio=0):
            for block in resampler.resample(frame):
                data = block.to_ndarray().ravel()
                end = position + block.samples
                if end > first and position < last:
                    wav.writeframesraw(data[max(0, first - position):last - position].astype("<i2").tobytes())
                position = end
            if position >= last:
                break
        if wav.getnframes() != last - first:
            raise ValueError("The excerpt WAV is shorter than expected.")


def reference_cues(path: Path) -> dict[str, tuple[str, str]]:
    document = path.read_text(encoding="utf-8", errors="replace")
    cues = {}
    for turn in TURNS:
        pattern = rf"^{re.escape(turn.mission)}\s+<font[^>]*>(\w+)</font><br>\s*(.*?)\s*<br><br>"
        matching = re.findall(pattern, document, flags=re.MULTILINE | re.DOTALL)
        if len(matching) != 1:
            raise ValueError(f"Could not locate exactly one NASA reference turn at {turn.mission}.")
        speaker, body = matching[0]
        cues[turn.name] = (speaker, " ".join(unescape(re.sub(r"<[^>]+>", "", body)).split()))
    return cues


def make_inputs(cache: Path, output: Path) -> tuple[Path, dict]:
    from review_app.transcription import timestamp

    mp3 = source(AUDIO, cache, AUDIO_SHA256)
    reference = reference_cues(source(TRANSCRIPT, cache, TRANSCRIPT_SHA256))
    inputs = output / "reference"
    inputs.mkdir()
    excerpt(mp3, inputs / "apollo-radio.wav")
    sections = []
    for turn in TURNS:
        talker, body = reference[turn.name]
        sections.append(f"{turn.name}\n{timestamp(turn.start_ms)} --> {timestamp(turn.end_ms)}\n"
                        f"<v {talker}>{escape(body, quote=False)}</v>")
    (inputs / "reference.vtt").write_text("WEBVTT\n\n" + "\n\n".join(sections) + "\n", encoding="utf-8")
    glossary = {"subjects": [{"subject_id": "separation-time", "label": "Separation time", "aliases": ["separation"],
                              "value_type": "elapsed_time"}]}
    (inputs / "glossary.json").write_text(json.dumps(glossary), encoding="utf-8")
    manifest = inputs / "exercise.json"
    manifest.write_text(json.dumps({
        "exercise_id": "apollo11-reference",
        "name": "Apollo 11 Flight Day 1 reference control",
        "glossary_file": "glossary.json",
        "sources": [{"source_key": "apollo-radio", "kind": "audio", "file": "apollo-radio.wav",
                     "transcript_file": "reference.vtt", "exercise_offset_ms": 0}],
    }, indent=2), encoding="utf-8")
    return manifest, reference


def overlap(locator: dict, turn: Turn) -> int:
    return max(0, min(locator["end_ms"], turn.end_ms) - max(locator["start_ms"], turn.start_ms))


def contains_turn(locator: dict, turn: Turn) -> bool:
    # Use the existing West Annex reviewed-citation tolerance for the held-out turns.
    return (abs(locator["start_ms"] - turn.start_ms) <= 500
            and abs(locator["end_ms"] - turn.end_ms) <= 1500
            and overlap(locator, turn) * 5 >= (turn.end_ms - turn.start_ms) * 3)


def score_citations(base: str, prefix: str, token: str, records: list[dict], wav: Path) -> list[dict]:
    checked = []
    for record in records:
        evidence = result(base, f"{prefix}/evidence/{record['evidence_id']}", token=token)
        locator = evidence["locator"]
        start, end = locator["start_ms"], locator["end_ms"]
        original = evidence["original_path"]
        link_valid = (
            evidence["source_sha256"] == source_hash(wav)
            and evidence["locator"] == record["locator"]
            and evidence["playback_path"] is not None
            and urldefrag(evidence["playback_path"]) == (original, f"t={start / 1000},{end / 1000}")
            and check_audio_bytes(base + original, wav, start, end, headers={"Authorization": f"Bearer {token}"})
        )
        checked.append({"evidence_id": record["evidence_id"], "locator": locator, "link_valid": link_valid,
                        "playback_path": evidence["playback_path"]})
    return checked


def score_links(events: list[dict], turn_citations: dict[str, str | None]) -> list[dict]:
    typed = {}
    for turn in TURNS:
        evidence_id = turn_citations[turn.name]
        typed[turn.name] = next((event for event in events
                                if evidence_id in event["evidence_ids"] and event["event_type"] == turn.kind), None)
    checks = []
    for start, end in LINKS:
        candidate, target = typed[start], typed[end]
        suggested = candidate is not None and target is not None and target["event_id"] in candidate["candidate_event_ids"]
        linked = candidate is not None and target is not None and any(
            edge["direction"] == "outgoing" and edge["event_id"] == target["event_id"]
            for edge in candidate["relationships"]
        )
        checks.append({"from": start, "to": end, "source_proposed": candidate is not None,
                       "target_proposed": target is not None, "suggested": suggested, "linked": linked})
    return checks


def score_times(records: list[dict]) -> dict:
    from review_app.reported_times import reported_time

    results = {}
    for turn in TURNS:
        if turn.name not in EXPECTED_TIMES:
            continue
        values = [parsed[0] for record in records if overlap(record["locator"], turn)
                  if (parsed := reported_time(record["excerpt"], ["separation"]))]
        results[turn.name] = {"expected": EXPECTED_TIMES[turn.name], "observed": values,
                              "matched": EXPECTED_TIMES[turn.name] in values}
    return results


def review_unique_correction(post, version: int, events: list[dict]) -> int | None:
    corrections = [item for item in events if item["event_type"] == "correction"
                   and item["payload"]["replacement_time_seconds"] is not None]
    if len(corrections) != 1 or len(corrections[0]["candidate_event_ids"]) != 1:
        return None
    correction = corrections[0]
    target = next(item for item in events if item["event_id"] == correction["candidate_event_ids"][0])
    if target["event_type"] != "time_report":
        return None
    # Simulate accepting the unique suggestion without inserting any answer-key text or times.
    for event, target_id in ((target, None), (correction, target["event_id"])):
        version = post(f"/review/events/{event['event_id']}", {
            "version": version, "subject_id": event["subject_id"], "state": "accepted", "target_id": target_id
        })["version"]
    return post("/review/publish", {"version": version})["number"]


def score_branch(base: str, token: str, exercise_id: str, reference: dict, wav: Path) -> dict:
    def post(path: str, payload: dict) -> dict:
        return result(base, f"/exercises/{exercise_id}{path}", token=token,
                      headers={"Content-Type": "application/json"}, data=json.dumps(payload).encode())

    draft = post("/review/extract", {})
    revision = post("/review/publish", {"version": draft["version"]})["number"]
    prefix = f"/v1/exercises/{exercise_id}/revisions/{revision}"
    records = result(base, prefix + "/records", token=token, params={"limit": 100})
    events = result(base, prefix + "/events", token=token, params={"limit": 100, "review_state": "all"})
    if records["next_cursor"] or events["next_cursor"]:
        raise ValueError("The benchmark excerpt exceeded one result page.")
    clips = score_citations(base, prefix, token, records["items"], wav)
    by_id = {record["evidence_id"]: record for record in records["items"]}
    targets = {}
    for turn in TURNS:
        target = max(clips, key=lambda clip: overlap(clip["locator"], turn), default=None)
        if target and not overlap(target["locator"], turn):
            target = None
        expected_words = words(reference[turn.name][1])
        actual_words = words(by_id[target["evidence_id"]]["excerpt"]) if target else []
        targets[turn.name] = {
            "expected_ms": [turn.start_ms, turn.end_ms], "mission_time": turn.mission,
            "closest_cue": target["locator"]["cue_id"] if target else None,
            "overlap_ms": overlap(target["locator"], turn) if target else 0,
            "turn_citation_correct": bool(target and target["link_valid"] and contains_turn(target["locator"], turn)),
            "matching_evidence_id": target["evidence_id"] if target and contains_turn(target["locator"], turn) else None,
            "reference_words": len(expected_words), "word_edits": edit_distance(expected_words, actual_words),
        }
    expected = words(" ".join(reference[turn.name][1] for turn in TURNS))
    generated = words(" ".join(record["excerpt"] for record in records["items"]))
    distance = edit_distance(expected, generated)
    reviewed = review_unique_correction(post, draft["version"], events["items"])
    answer_revision = reviewed or revision
    assistant = post("/assistant/ask", {"revision_id": answer_revision,
                                       "question": "What was the correction to the separation time?"})
    answer_clips = score_citations(base, f"/v1/exercises/{exercise_id}/revisions/{answer_revision}",
                                  token, assistant["citations"], wav)
    verified_turns = [turn.name for turn in TURNS for clip in answer_clips
                      if clip["link_valid"] and contains_turn(clip["locator"], turn)]
    by_turn = {name: detail["matching_evidence_id"] for name, detail in targets.items()}
    links = score_links(events["items"], by_turn)
    negative_control = None
    if reviewed:
        accepted = result(base, f"/v1/exercises/{exercise_id}/revisions/{reviewed}/events", token=token)["items"]
        linked = score_links(accepted, by_turn)
        for before, after in zip(links, linked, strict=True):
            before["linked"] = after["linked"]
        original = assistant["claim"]["original_time_seconds"]
        if original is not None:
            negative_control = post("/assistant/verify", {**assistant["claim"], "original_time_seconds": original - 1})["status"]
    return {
        "asr": {"reference_words": len(expected), "hypothesis_words": len(generated),
                "word_edits": distance, "word_error_rate": round(distance / len(expected), 6)},
        "clips": clips, "turns": targets, "announced_times": score_times(records["items"]),
        "events": [{"kind": event["event_type"], "state": event["review_state"], "evidence_ids": event["evidence_ids"]}
                   for event in events["items"]],
        "links": links,
        "assistant": {"verification": assistant["verification"]["status"], "answer": assistant["answer"],
                      "revision": answer_revision, "citations": answer_clips, "matching_turns": verified_turns,
                      "wrong_value_control": negative_control,
                      "times_seconds": [assistant["claim"]["original_time_seconds"], assistant["claim"]["revised_time_seconds"]]},
    }


def evaluate(output: Path, cache: Path, browser_check: Path | None, *, reference_only: bool = False,
             model: str = "small.en", profile: str = "standard") -> dict:
    from review_app import pilot_admin
    from review_app.importer import import_exercise
    from review_app.storage import connect_owner, migrate
    from review_app.transcription import build_bundle

    with connect_owner() as conn:
        if conn.execute("SELECT 1 FROM pg_tables WHERE schemaname = 'public' LIMIT 1").fetchone():
            raise ValueError("Use a new disposable database for this benchmark.")
    manifest, reference = make_inputs(cache, output)
    wav = manifest.parent / "apollo-radio.wav"
    performance = None
    branches = {"reference_control": manifest}
    if not reference_only:
        raw = output / "raw"
        performance = build_bundle(manifest, raw, exercise_id="apollo11-raw", offline=True,
                                   model_name=model, profile=profile)
        if source_hash(raw / "sources" / "apollo-radio.wav") != source_hash(wav):
            raise ValueError("Roxi transcription changed the excerpt WAV.")
        branches["raw"] = raw / "exercise.json"
    migrate()
    pilot_admin.workspace("apollo11", "Apollo 11 archived audio benchmark")
    username, password = "apollo-reviewer", secrets.token_urlsafe(24)
    pilot_admin.set_password(username, password)
    pilot_admin.member("apollo11", username, "reviewer", all_sources=True)
    exercises = {}
    for name, path in branches.items():
        imported = import_exercise(path)
        pilot_admin.attach("apollo11", imported.exercise_id)
        exercises[name] = imported.exercise_id
    token = pilot_admin.issue_token(username)
    with local_server(pilot=True) as base:
        results = {name: score_branch(base, token, exercise, reference, wav)
                   for name, exercise in exercises.items()}
        if browser_check:
            paths = {name: branch["clips"] for name, branch in results.items()}
            paths.update({name + "_answer": branch["assistant"]["citations"] for name, branch in results.items()})
            env = dict(os.environ, ROXI_BENCHMARK_BASE=base, ROXI_BENCHMARK_USERNAME=username,
                       ROXI_BENCHMARK_PASSWORD=password, ROXI_BENCHMARK_CLIPS=json.dumps(paths))
            browser = subprocess.run(["node", str(browser_check.resolve())], env=env, capture_output=True, text=True,
                                     check=True)
            results["browser"] = json.loads(browser.stdout)
    return {"audio": AUDIO, "reference": TRANSCRIPT, "nasa_mp3_sha256": AUDIO_SHA256,
            "excerpt_ms": [START_MS, END_MS], "excerpt_sha256": source_hash(wav), "transcription": performance,
            **results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path(".local/apollo11-day1"))
    parser.add_argument("--output", type=Path, required=True, help="New output directory for the evaluation run.")
    parser.add_argument("--browser-check", type=Path, help="Optional Node script that tests the emitted playback URLs.")
    parser.add_argument("--reference-only", action="store_true", help="Verify correction linking without running speech recognition.")
    parser.add_argument("--model", default="small.en", help="Locally cached Faster Whisper model.")
    parser.add_argument("--profile", choices=("standard", "radio"), default="standard")
    args = parser.parse_args()
    if any(not os.environ.get(name) for name in DATABASE_SETTINGS):
        parser.error("Set all three database URLs to a new disposable database.")
    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True)
    os.environ["EXERCISE_REVIEW_ARCHIVE"] = str(args.output.resolve() / "archive")
    os.environ["EXERCISE_REVIEW_CHAT_MODEL"] = ""
    os.environ.pop("EXERCISE_REVIEW_API_EXERCISES", None)
    report = evaluate(args.output, args.cache, args.browser_check, reference_only=args.reference_only,
                      model=args.model, profile=args.profile)
    encoded = json.dumps(report, indent=2) + "\n"
    (args.output / "report.json").write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
