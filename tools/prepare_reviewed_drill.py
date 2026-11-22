"""Apply the West Annex reviewer cue map to the original automatic transcript bundle."""

import argparse
import csv
import shutil
import sys
import wave
from collections import defaultdict
from filecmp import cmp
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "drill-01"
sys.path.insert(0, str(ROOT))

from review_app.importer import AudioSource, Manifest, file_in, parse_vtt
from review_app.transcription import timestamp


def reviewed_vtt(bundle: Path, source: AudioSource, decisions: list[dict]) -> str:
    with wave.open(str(file_in(bundle, source.file)), "rb") as recording:
        duration = round(recording.getnframes() * 1000 / recording.getframerate())
    raw = parse_vtt(file_in(bundle, source.transcript_file).read_text(encoding="utf-8-sig"), duration, None)
    by_id = {cue.cue_id: cue for cue in raw}
    mapped = [key for row in decisions for key in row["generated_cues"].split()]
    if mapped != [cue.cue_id for cue in raw] or len(by_id) != len(raw):
        raise ValueError(f"The review map must cover the generated cues in order for {source.source_key}.")
    bodies = {
        row["cue_id"]: " ".join(by_id[key].body for key in row["generated_cues"].split())
        for row in decisions
    }
    if source.source_key == "facilities":
        # The recognizer grouped the next speaker's call sign with the Bay 3 report.
        report, gap, call_sign = bodies["F06"].rpartition(" ")
        if not gap or call_sign.casefold() != "facilities" or not bodies["F07"].casefold().startswith("to dispatch"):
            raise ValueError("The reviewed call-sign move needs to be checked against this generated transcript.")
        bodies["F06"] = report
        bodies["F07"] = call_sign + " " + bodies["F07"]

    sections = ["WEBVTT"]
    prior_end = 0
    for row in decisions:
        cue_id = row["cue_id"]
        # Leave room for quiet edges around the measured speech in the original recording.
        start = max(0, int(row["onset_ms"]) - 100)
        end = min(duration, int(row["offset_ms"]) + 200)
        if not prior_end <= start < end:
            raise ValueError(f"Invalid reviewed audio interval for {cue_id}.")
        for key in row["generated_cues"].split():
            cue = by_id[key]
            if cue.audio_end_ms <= start or cue.audio_start_ms >= end:
                raise ValueError(f"Recheck {source.source_key} {key}. It no longer overlaps {cue_id}.")
        speaker = escape(row["speaker"])
        body = escape(bodies[cue_id].strip(" ,.!?"), quote=False)
        sections.append(f"{cue_id}\n{timestamp(start)} --> {timestamp(end)}\n<v {speaker}>{body}</v>")
        prior_end = end
    return "\n\n".join(sections) + "\n"


def prepare(bundle: Path, output: Path) -> None:
    generated = Manifest.model_validate_json(file_in(bundle, "exercise.json").read_bytes())
    original_root = FIXTURE / "input"
    originals = Manifest.model_validate_json((original_root / "exercise.json").read_bytes())
    original_files = {source.source_key: original_root / source.file for source in originals.sources}
    if sorted(source.source_key for source in generated.sources) != sorted(original_files):
        raise ValueError("The generated bundle must include each West Annex source exactly once.")
    decisions = defaultdict(list)
    with (FIXTURE / "reference" / "asr-review.tsv").open(encoding="utf-8", newline="") as review:
        for row in csv.DictReader(review, delimiter="\t"):
            decisions[row["source_key"]].append(row)
    audio_keys = {source.source_key for source in generated.sources if isinstance(source, AudioSource)}
    if audio_keys != decisions.keys():
        raise ValueError("The review map must cover the two original drill recordings.")
    if output.exists():
        raise ValueError(f"Output already exists: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".reviewed-drill-", dir=output.parent) as temp:
        working = Path(temp) / "bundle"
        working.mkdir()
        shutil.copyfile(file_in(bundle, generated.glossary_file), working / "glossary.json")
        generated.glossary_file = "glossary.json"
        generated.name = "West Annex dock flow drill (reviewed automatic transcript)"
        for source in generated.sources:
            original = original_files[source.source_key]
            generated_media = file_in(bundle, source.file)
            name = original.name
            if not cmp(generated_media, original, shallow=False):
                raise ValueError(f"The generated bundle changed the original {name}.")
            shutil.copyfile(generated_media, working / name)
            if isinstance(source, AudioSource):
                transcript = Path(name).with_suffix(".vtt").name
                (working / transcript).write_text(reviewed_vtt(bundle, source, decisions[source.source_key]),
                                                  encoding="utf-8", newline="\n")
                source.transcript_file = transcript
            source.file = name
        (working / "exercise.json").write_text(generated.model_dump_json(indent=2) + "\n", encoding="utf-8")
        working.rename(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("generated_bundle", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        prepare(args.generated_bundle.resolve(strict=True), args.output.resolve())
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
