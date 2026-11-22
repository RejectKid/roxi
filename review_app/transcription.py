import json
import os
import shutil
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from .importer import AudioSource, Manifest, file_in
from .storage import ROOT

DEFAULT_MODEL = "small.en"
CACHE = ROOT / ".local" / "models"


def timestamp(milliseconds: int) -> str:
    seconds, fraction = divmod(milliseconds, 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{fraction:03}"


def split_at_silence(segment):
    if not segment.words:
        yield segment.start, segment.end, segment.text
        return
    words = []
    for word in segment.words:
        if words and word.start - words[-1].end > 1.0:
            yield words[0].start, words[-1].end, "".join(item.word for item in words)
            words = []
        words.append(word)
    if words:
        yield words[0].start, words[-1].end, "".join(item.word for item in words)


def transcribe_recording(model, source: Path, language: str) -> tuple[str, dict]:
    started = perf_counter()
    segments, info = model.transcribe(
        str(source), language=language, beam_size=5, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 750, "speech_pad_ms": 200},
        condition_on_previous_text=False, word_timestamps=True,
    )
    duration_ms = round(info.duration * 1000)
    cues = []
    for segment in segments:
        for start, end, text in split_at_silence(segment):
            start_ms = max(0, round(start * 1000))
            end_ms = min(duration_ms, round(end * 1000))
            body = " ".join(text.split())
            if body and start_ms < end_ms:
                cues.append((start_ms, end_ms, body))
    sections = [
        f"S{number:03}\n{timestamp(start)} --> {timestamp(end)}\n{escape(text, quote=False)}"
        for number, (start, end, text) in enumerate(sorted(cues), 1)
    ]
    return "WEBVTT\n\n" + "\n\n".join(sections) + "\n", {
        "duration_ms": duration_ms, "cues": len(sections), "seconds": round(perf_counter() - started, 2),
    }


def build_bundle(manifest_path: Path, output: Path, *, model_name: str = DEFAULT_MODEL,
                 language: str = "en", exercise_id: str | None = None, offline: bool = False) -> dict:
    from faster_whisper import WhisperModel

    manifest_path = manifest_path.resolve(strict=True)
    source_root = manifest_path.parent
    output = output.resolve()
    if output.exists():
        raise ValueError(f"Output already exists: {output}")
    manifest = Manifest.model_validate_json(manifest_path.read_bytes())
    if len({source.source_key for source in manifest.sources}) != len(manifest.sources):
        raise ValueError("Source keys must be unique within an exercise.")
    generated = manifest.model_dump(mode="json")
    if exercise_id:
        generated["exercise_id"] = exercise_id
        generated["name"] += " (automatic transcript)"
    Manifest.model_validate(generated)
    if not any(isinstance(source, AudioSource) for source in manifest.sources):
        raise ValueError("The manifest has no recordings to transcribe.")

    threads = min(8, os.cpu_count() or 1)
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads,
                         download_root=str(CACHE), local_files_only=offline)
    report = {"model": model_name, "engine": "faster-whisper", "device": "cpu", "compute_type": "int8",
              "language": language, "tracks": {}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".transcription-", dir=output.parent) as working:
        bundle = Path(working) / "bundle"
        sources = bundle / "sources"
        sources.mkdir(parents=True)
        glossary = file_in(source_root, manifest.glossary_file)
        shutil.copyfile(glossary, bundle / "glossary.json")
        generated["glossary_file"] = "glossary.json"

        for metadata, source in zip(generated["sources"], manifest.sources, strict=True):
            original = file_in(source_root, source.file)
            suffix = ".wav" if isinstance(source, AudioSource) else ".irc"
            filename = source.source_key + suffix
            shutil.copyfile(original, sources / filename)
            metadata["file"] = f"sources/{filename}"
            if isinstance(source, AudioSource):
                transcript = f"sources/{source.source_key}.vtt"
                vtt, details = transcribe_recording(model, sources / filename, language)
                (bundle / transcript).write_text(vtt, encoding="utf-8")
                metadata["transcript_file"] = transcript
                report["tracks"][source.source_key] = details

        for name, content in (("exercise.json", generated), ("transcription.json", report)):
            (bundle / name).write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
        bundle.rename(output)
    return report
