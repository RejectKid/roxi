"""Score raw local transcription and published-revision answers on the West Annex drill."""

import argparse
import hashlib
import json
import re
import sys
import wave
from pathlib import Path
from time import perf_counter
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "fixtures" / "drill-01"
sys.path.insert(0, str(ROOT))

from review_app.extraction import subject_entries
from review_app.importer import parse_vtt

WORDS = re.compile(r"[a-z0-9]+")
OOS = re.compile(r"\b(?:oos|o[\s.]+o[\s.]+s|out of service|not in service|closed for returns)\b", re.I)
IN_SERVICE = re.compile(r"\b(?<!not )(?:in service|open for returns)\b", re.I)


def words(text: str) -> list[str]:
    return WORDS.findall(text.casefold())


def edit_distance(reference: list[str], generated: list[str]) -> int:
    previous = list(range(len(generated) + 1))
    for row, token in enumerate(reference, 1):
        current = [row]
        for col, other in enumerate(generated, 1):
            current.append(min(previous[col] + 1, current[col - 1] + 1, previous[col - 1] + (token != other)))
        previous = current
    return previous[-1]


def critical_terms(text: str, glossary: dict) -> set[str]:
    normalized = " ".join(words(text))
    padded = f" {normalized} "
    found = {entry["subject_id"] for entry in subject_entries(glossary)
             if any(f" {' '.join(words(alias))} " in padded
                    for alias in [entry["label"], *entry["aliases"]])}
    if OOS.search(text):
        found.add("out_of_service")
    if IN_SERVICE.search(text):
        found.add("in_service")
    return found


def score_asr(bundle: Path) -> dict:
    root = REFERENCE / "input"
    reference = json.loads((root / "exercise.json").read_text(encoding="utf-8"))
    generated = json.loads((bundle / "exercise.json").read_text(encoding="utf-8"))
    by_key = {entry["source_key"]: entry for entry in generated["sources"]}
    glossary = json.loads((root / "glossary.json").read_text(encoding="utf-8"))
    tracks = {}
    for source in reference["sources"]:
        expected = root / source["file"]
        produced = bundle / by_key[source["source_key"]]["file"]
        with expected.open("rb") as left, produced.open("rb") as right:
            if hashlib.file_digest(left, "sha256").digest() != hashlib.file_digest(right, "sha256").digest():
                raise ValueError("The evaluation bundle changed an original source file.")
        if source["kind"] != "audio":
            continue
        with wave.open(str(expected), "rb") as audio:
            duration = round(audio.getnframes() * 1000 / audio.getframerate())
        gold = parse_vtt((root / source["transcript_file"]).read_text(encoding="utf-8"), duration, 0)
        actual = parse_vtt((bundle / by_key[source["source_key"]]["transcript_file"]).read_text(encoding="utf-8"), duration, 0)
        ref_tokens = words(" ".join(record.body for record in gold))
        asr_tokens = words(" ".join(record.body for record in actual))
        possible = correct = 0
        misses = []
        for cue in gold:
            terms = critical_terms(cue.body, glossary)
            text = " ".join(record.body for record in actual
                            if record.audio_start_ms < cue.audio_end_ms + 500
                            and record.audio_end_ms > cue.audio_start_ms - 500)
            recognized = critical_terms(text, glossary)
            possible += len(terms)
            correct += len(terms & recognized)
            if terms - recognized:
                misses.append({"cue": cue.cue_id, "expected": cue.body, "generated": text,
                               "missing_terms": sorted(terms - recognized)})
        distance = edit_distance(ref_tokens, asr_tokens)
        tracks[source["source_key"]] = {
            "reference_words": len(ref_tokens), "generated_words": len(asr_tokens), "word_edits": distance,
            "wer": round(distance / len(ref_tokens), 4), "critical_terms_recognized": correct,
            "critical_terms_total": possible, "misses": misses,
        }
    edits = sum(track["word_edits"] for track in tracks.values())
    total = sum(track["reference_words"] for track in tracks.values())
    return {"tracks": tracks, "word_edits": edits, "reference_words": total, "wer": round(edits / total, 4),
            "critical_terms_recognized": sum(track["critical_terms_recognized"] for track in tracks.values()),
            "critical_terms_total": sum(track["critical_terms_total"] for track in tracks.values())}


def http(base: str, path: str, payload: dict | None = None) -> dict:
    request = Request(base + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"} if payload is not None else {})
    with urlopen(request, timeout=75) as response:
        return json.load(response)


def check_original(base: str, item: dict, expected: dict) -> bool:
    locator = item["locator"]
    if (item["original_name"], item["capture_offset_ms"], item["original_excerpt"]) != (
        expected["file"], expected["exercise_start_ms"], expected["quote"]
    ) or locator["kind"] != expected["kind"]:
        return False
    url = base + item["original_path"]
    if locator["kind"] == "audio_interval":
        if any(locator[key] != expected[key] for key in ("start_ms", "end_ms", "cue_id")):
            return False
        with urlopen(Request(url, headers={"Range": "bytes=0-11"}), timeout=10) as audio:
            header = audio.read()
            return audio.status == 206 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    if any(locator[key] != expected[key] for key in ("line_start", "line_end")):
        return False
    with urlopen(url, timeout=10) as chat:
        for number, line in enumerate(chat, 1):
            if number == locator["line_start"]:
                return line.decode("utf-8-sig").rstrip("\r\n") == item["original_text"]
    return False


def score_chat(base: str, exercise_id: str, revision: int) -> dict:
    answer_key = json.loads((REFERENCE / "reference" / "answer-key.json").read_text(encoding="utf-8"))
    path = f"/exercises/{exercise_id}/assistant"
    results = []
    for case in answer_key["cases"]:
        started = perf_counter()
        response = http(base, path + "/ask", {"revision_id": revision, "question": case["question"]})
        claim = response["claim"]
        expected = case["expected"]
        all_expected = [*case["evidence_ids"], *case.get("coverage_evidence_ids", [])]
        names = {}
        for citation in response["citations"]:
            for key in all_expected:
                gold = answer_key["evidence"][key]
                if (citation["locator"].get("cue_id"), citation["locator"].get("line_start"), citation["original_name"]) != (
                    gold.get("cue_id"), gold.get("line_start"), gold["file"]
                ):
                    continue
                if check_original(base, citation, gold):
                    names[citation["evidence_id"]] = key
                break

        errors = []
        if re.search(r"\b\d{1,4}:[0-5]\d\b", case["question"]) and claim["as_of_ms"] != case["as_of_ms"]:
            errors.append(f"cutoff expected {case['as_of_ms']}, got {claim['as_of_ms']}")
        for field, expected_value in expected.items():
            if field == "draft_subject_id":
                continue
            actual = claim[field]
            if field == "target_evidence_id":
                actual = names.get(actual)
            if field == "candidate_evidence_ids":
                actual = sorted(names.get(value, value) for value in actual)
                expected_value = sorted(expected_value)
            if actual != expected_value:
                errors.append(f"{field} expected {expected_value}, got {actual}")
        required = set(case["evidence_ids"])
        primary = {names.get(key, key) for key in claim["evidence_ids"]}
        expected_coverage = set(case.get("coverage_evidence_ids", []))
        coverage = {names.get(key, key) for key in claim["coverage_evidence_ids"]}
        citations_match = primary == required and coverage == expected_coverage
        results.append({
            "case": case["id"], "answer_matches": not errors, "citations_match": citations_match,
            "verification": response["verification"]["status"], "elapsed_seconds": round(perf_counter() - started, 2),
            "answer": response["answer"], "parser": response["parser"], "errors": errors,
            "returned_citations": sorted(primary | coverage),
        })
        if case["id"] == "first-report":
            first_claim = claim

    wrong_subject = http(base, path + "/verify", {**first_claim, "subject_id": "bay-1"})
    wrong_locator = http(base, path + "/verify", {**first_claim, "evidence_ids": ["s999999-r1"]})
    return {
        "revision_id": revision, "cases": results,
        "answers_matched": sum(row["answer_matches"] for row in results),
        "citations_matched": sum(row["citations_match"] for row in results),
        "verified": sum(row["verification"] == "supported" for row in results),
        "wrong_subject_control": wrong_subject["status"], "invalid_locator_control": wrong_locator["status"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asr-bundle", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--exercise", default="drill-01")
    parser.add_argument("--revision", type=int, help="Published revision of the reference drill to score")
    parser.add_argument("--output", type=Path, help="Also save the JSON report to this path")
    args = parser.parse_args()
    if not args.asr_bundle and args.revision is None:
        parser.error("provide --asr-bundle, --revision, or both")
    report = {}
    if args.asr_bundle:
        report["transcription"] = score_asr(args.asr_bundle.resolve())
    if args.revision is not None:
        report["chat"] = score_chat(args.url.rstrip("/"), args.exercise, args.revision)
    output = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    if "chat" in report:
        chat = report["chat"]
        if not (chat["answers_matched"] == chat["citations_matched"] == chat["verified"] == len(chat["cases"])
                and chat["wrong_subject_control"] == "contradicted"
                and chat["invalid_locator_control"] == "insufficient_evidence"):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
