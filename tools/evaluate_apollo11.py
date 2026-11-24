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
    Turn("original-report", "00 02 54 09", 1420, 19230, "status_report"),
    Turn("crew-readback", "00 02 54 33", 25420, 37940, "acknowledgment"),
    Turn("correction", "00 02 54 46", 38510, 43080, "correction"),
    Turn("crew-acknowledgment", "00 02 54 55", 47750, 48260, "acknowledgment"),
)
LINKS = (
    ("crew-readback", "original-report"),
    ("correction", "original-report"),
    ("crew-acknowledgment", "correction"),
)
TIME_VALUE = re.compile(r"\bseparation\s+(?:at|should be)\s+(\d+(?:\s*(?:plus\s*)?\d+)*)", re.I)


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
    glossary = {"subjects": [{"subject_id": "separation-time", "label": "Separation time", "aliases": ["separation"]}]}
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
    expected = {"original-report": "31500", "correction": "31503"}
    results = {}
    for turn in TURNS:
        if turn.name not in expected:
            continue
        text = " ".join(record["excerpt"] for record in records if overlap(record["locator"], turn))
        values = ["".join(re.findall(r"\d", match)) for match in TIME_VALUE.findall(text)]
        results[turn.name] = {"expected": expected[turn.name], "observed": values,
                              "matched": expected[turn.name] in values}
    return results


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
    targets = {}
    for turn in TURNS:
        target = max(clips, key=lambda clip: overlap(clip["locator"], turn), default=None)
        if target and not overlap(target["locator"], turn):
            target = None
        targets[turn.name] = {
            "expected_ms": [turn.start_ms, turn.end_ms], "mission_time": turn.mission,
            "closest_cue": target["locator"]["cue_id"] if target else None,
            "overlap_ms": overlap(target["locator"], turn) if target else 0,
            "turn_citation_correct": bool(target and target["link_valid"] and contains_turn(target["locator"], turn)),
            "matching_evidence_id": target["evidence_id"] if target and contains_turn(target["locator"], turn) else None,
        }
    expected = words(" ".join(reference[turn.name][1] for turn in TURNS))
    generated = words(" ".join(record["excerpt"] for record in records["items"]))
    distance = edit_distance(expected, generated)
    assistant = post("/assistant/ask", {"revision_id": revision,
                                       "question": "What was the correction to the separation time?"})
    return {
        "asr": {"reference_words": len(expected), "hypothesis_words": len(generated),
                "word_edits": distance, "word_error_rate": round(distance / len(expected), 6)},
        "clips": clips, "turns": targets, "announced_times": score_times(records["items"]),
        "events": [{"kind": event["event_type"], "state": event["review_state"], "evidence_ids": event["evidence_ids"]}
                   for event in events["items"]],
        "links": score_links(events["items"], {name: turn["matching_evidence_id"] for name, turn in targets.items()}),
        "assistant": {"verification": assistant["verification"]["status"], "answer": assistant["answer"],
                      "citations": [item["evidence_id"] for item in assistant["citations"]]},
    }


def evaluate(output: Path, cache: Path, browser_check: Path | None) -> dict:
    from review_app import pilot_admin
    from review_app.importer import import_exercise
    from review_app.storage import connect_owner, migrate
    from review_app.transcription import build_bundle

    with connect_owner() as conn:
        if conn.execute("SELECT 1 FROM pg_tables WHERE schemaname = 'public' LIMIT 1").fetchone():
            raise ValueError("Use a new disposable database for this benchmark.")
    manifest, reference = make_inputs(cache, output)
    raw = output / "raw"
    performance = build_bundle(manifest, raw, exercise_id="apollo11-raw", offline=True)
    wav = manifest.parent / "apollo-radio.wav"
    if source_hash(raw / "sources" / "apollo-radio.wav") != source_hash(wav):
        raise ValueError("Roxi transcription changed the excerpt WAV.")
    migrate()
    pilot_admin.workspace("apollo11", "Apollo 11 archived audio benchmark")
    username, password = "apollo-reviewer", secrets.token_urlsafe(24)
    pilot_admin.set_password(username, password)
    pilot_admin.member("apollo11", username, "reviewer", all_sources=True)
    for path in (raw / "exercise.json", manifest):
        imported = import_exercise(path)
        pilot_admin.attach("apollo11", imported.exercise_id)
    token = pilot_admin.issue_token(username)
    with local_server(pilot=True) as base:
        results = {name: score_branch(base, token, exercise, reference, wav)
                   for name, exercise in (("raw", "apollo11-raw"), ("reference_control", "apollo11-reference"))}
        if browser_check:
            paths = {name: branch["clips"] for name, branch in results.items()}
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
    args = parser.parse_args()
    if any(not os.environ.get(name) for name in DATABASE_SETTINGS):
        parser.error("Set all three database URLs to a new disposable database.")
    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True)
    os.environ["EXERCISE_REVIEW_ARCHIVE"] = str(args.output.resolve() / "archive")
    os.environ["EXERCISE_REVIEW_CHAT_MODEL"] = ""
    os.environ.pop("EXERCISE_REVIEW_API_EXERCISES", None)
    report = evaluate(args.output, args.cache, args.browser_check)
    encoded = json.dumps(report, indent=2) + "\n"
    (args.output / "report.json").write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
