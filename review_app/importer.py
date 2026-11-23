import hashlib
import json
import re
import wave
from dataclasses import astuple, dataclass
from html import unescape
from pathlib import Path
from typing import Annotated, Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, NonNegativeInt, StringConstraints

from .storage import OWNER_DSN, ArchivedFile, archive_file, archive_path, archive_writer

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=96)]
TIMESTAMP = r"(?:\d{2,}:)?[0-5]\d:[0-5]\d\.\d{3}"
CUE_TIMING = re.compile(rf"^(?P<start>{TIMESTAMP})\s+-->\s+(?P<end>{TIMESTAMP})(?:\s+.*)?$")
VOICE_TAG = re.compile(r"^<v(?:\.[^\s>]+)?\s+([^>]+)>(.*?)(?:</v>)?$", re.DOTALL)
CHAT_LINE = re.compile(r"^\[(\d{2,}:[0-5]\d:[0-5]\d)\] <([^<>]+)> (.+)$")
CHAT_LINE_MS = re.compile(r"^\[(\d{2,}:[0-5]\d:[0-5]\d\.\d{3})\] <([^<>]+)> (.+)$")
PARSER_VERSION = 1


class AudioSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_key: Slug
    kind: Literal["audio"]
    file: str
    transcript_file: str
    exercise_offset_ms: NonNegativeInt | None = None


class ChatSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_key: Slug
    kind: Literal["chat"]
    file: str
    time_basis: Literal["exercise_elapsed", "exercise_elapsed_ms"]


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exercise_id: Slug
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    started_at_utc: AwareDatetime | None = None
    glossary_file: str
    sources: list[Annotated[AudioSource | ChatSource, Field(discriminator="kind")]] = Field(min_length=1)


@dataclass(frozen=True)
class Record:
    ordinal: int
    cue_id: str | None
    speaker: str | None
    body: str
    original: str
    capture_ms: int | None
    audio_start_ms: int | None = None
    audio_end_ms: int | None = None


@dataclass(frozen=True)
class Issue:
    line_number: int
    reason: str
    original: str


@dataclass(frozen=True)
class PreparedSource:
    source: AudioSource | ChatSource
    filename: str
    raw: ArchivedFile
    profile: str
    transcript_name: str | None
    transcript: ArchivedFile | None
    duration_ms: int | None
    records: list[Record]
    issues: list[Issue]


@dataclass(frozen=True)
class ImportSummary:
    exercise_id: str
    sources: int
    new_source_versions: int
    records: int
    issues: int


def file_in(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or candidate.drive:
        raise ValueError(f"Expected a path relative to the exercise folder: {relative}")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"Exercise input is missing or outside its folder: {relative}")
    return resolved


def read_text(archived: ArchivedFile) -> str:
    return archive_path(archived.sha256).read_text(encoding="utf-8-sig")


def elapsed_ms(value: str) -> int:
    clock, _, fractional = value.partition(".")
    seconds = 0
    for part in clock.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds * 1000 + int(fractional or 0)


def parse_chat(text: str, *, milliseconds: bool = False) -> tuple[list[Record], list[Issue]]:
    records, issues = [], []
    pattern = CHAT_LINE_MS if milliseconds else CHAT_LINE
    clock = "HH:MM:SS.mmm" if milliseconds else "HH:MM:SS"
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        match = pattern.fullmatch(line)
        if not match:
            issues.append(Issue(number, f"Expected [{clock}] <speaker> message.", line))
            continue
        stamp, speaker, message = match.groups()
        records.append(Record(number, None, speaker, message, line, elapsed_ms(stamp)))
    return records, issues


def parse_vtt(text: str, duration_ms: int, offset_ms: int | None) -> list[Record]:
    header, *blocks = re.split(r"\n[ \t]*\n", text.strip())
    if not re.match(r"^WEBVTT(?:[ \t\n]|$)", header):
        raise ValueError("Transcript needs a WEBVTT header.")
    records = []
    for block in blocks:
        lines = block.splitlines()
        if not lines or lines[0].split(" ", 1)[0] in {"NOTE", "STYLE", "REGION"}:
            continue
        cue_id = None
        if "-->" not in lines[0]:
            cue_id = lines.pop(0)
        timing = CUE_TIMING.fullmatch(lines[0]) if lines else None
        if not timing or len(lines) < 2:
            raise ValueError(f"Invalid WebVTT cue {cue_id or len(records) + 1}.")
        start_ms, end_ms = (elapsed_ms(stamp) for stamp in timing.groups())
        if not start_ms < end_ms <= duration_ms:
            raise ValueError(f"WebVTT cue {cue_id or len(records) + 1} is outside the audio.")
        original = "\n".join(lines[1:])
        voice = VOICE_TAG.fullmatch(original)
        speaker, content = voice.groups() if voice else (None, original)
        body = unescape(re.sub(r"</?[^<>]+>", "", content))
        capture_ms = start_ms + offset_ms if offset_ms is not None else None
        records.append(
            Record(len(records) + 1, cue_id, unescape(speaker) if speaker else None,
                   body, original, capture_ms, start_ms, end_ms)
        )
    return records


def wave_duration(archived: ArchivedFile) -> int:
    try:
        with wave.open(str(archive_path(archived.sha256)), "rb") as audio:
            frame_count = audio.getnframes()
            frame_bytes = audio.getnchannels() * audio.getsampwidth()
            if audio.getnchannels() not in {1, 2} or not frame_count:
                raise ValueError("Expected a nonempty mono or stereo PCM WAV.")
            remaining = frame_count
            while remaining:
                frames = min(remaining, 65536)
                if len(audio.readframes(frames)) != frames * frame_bytes:
                    raise ValueError("The WAV payload is shorter than its header indicates.")
                remaining -= frames
            return round(frame_count * 1000 / audio.getframerate())
    except (wave.Error, EOFError) as exc:
        raise ValueError("Expected a valid PCM WAV recording.") from exc


def prepare_source(root: Path, source: AudioSource | ChatSource) -> PreparedSource:
    media_path = file_in(root, source.file)
    raw = archive_file(media_path)
    profile = {"parser": PARSER_VERSION, **source.model_dump()}
    if isinstance(source, AudioSource):
        transcript_path = file_in(root, source.transcript_file)
        transcript = archive_file(transcript_path)
        duration = wave_duration(raw)
        records = parse_vtt(read_text(transcript), duration, source.exercise_offset_ms)
        issues = []
        profile["transcript_sha256"] = transcript.sha256
    else:
        transcript_path = transcript = duration = None
        records, issues = parse_chat(read_text(raw), milliseconds=source.time_basis == "exercise_elapsed_ms")
    profile_sha = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    return PreparedSource(source, media_path.name, raw, profile_sha,
                          transcript_path.name if transcript_path else None, transcript, duration, records, issues)


def save_source(conn, exercise_id: str, item: PreparedSource) -> tuple[int, bool]:
    source = item.source
    row = conn.execute(
        """
        INSERT INTO source_file (
            exercise_id, source_key, kind, original_name, content_sha256, byte_size,
            import_profile_sha256, transcript_name, transcript_sha256, exercise_offset_ms, duration_ms
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (exercise_id, source_key, content_sha256, import_profile_sha256) DO NOTHING
        RETURNING id
        """,
        (
            exercise_id, source.source_key, source.kind, item.filename, item.raw.sha256, item.raw.byte_size,
            item.profile, item.transcript_name, item.transcript.sha256 if item.transcript else None,
            source.exercise_offset_ms if isinstance(source, AudioSource) else None, item.duration_ms,
        ),
    ).fetchone()
    if row is None:
        current = conn.execute(
            "SELECT id FROM source_file WHERE exercise_id = %s AND source_key = %s "
            "AND content_sha256 = %s AND import_profile_sha256 = %s",
            (exercise_id, source.source_key, item.raw.sha256, item.profile),
        ).fetchone()
        return current[0], False

    source_id = row[0]
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO source_record (source_file_id, ordinal, cue_id, speaker, body, original_text, "
            "capture_offset_ms, audio_start_ms, audio_end_ms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ((source_id, *astuple(record)) for record in item.records),
        )
        cursor.executemany(
            "INSERT INTO import_issue (source_file_id, line_number, reason, original_text) VALUES (%s, %s, %s, %s)",
            ((source_id, *astuple(issue)) for issue in item.issues),
        )
    return source_id, True


@archive_writer
def import_exercise(manifest_path: Path) -> ImportSummary:
    manifest_path = manifest_path.resolve(strict=True)
    manifest_file = archive_file(manifest_path)
    manifest = Manifest.model_validate_json(read_text(manifest_file))
    if len({source.source_key for source in manifest.sources}) != len(manifest.sources):
        raise ValueError("Source keys must be unique within an exercise.")
    root = manifest_path.parent
    glossary_file = archive_file(file_in(root, manifest.glossary_file))
    glossary = json.loads(read_text(glossary_file))
    if not isinstance(glossary, dict):
        raise ValueError("Exercise glossary must be a JSON object.")
    items = [prepare_source(root, source) for source in manifest.sources]

    new_count = 0
    with psycopg.connect(OWNER_DSN, connect_timeout=5) as conn:
        conn.execute(
            """
            INSERT INTO exercise (id, name, started_at_utc, manifest_sha256, glossary_sha256, glossary)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name, started_at_utc = EXCLUDED.started_at_utc,
                manifest_sha256 = EXCLUDED.manifest_sha256, glossary_sha256 = EXCLUDED.glossary_sha256,
                glossary = EXCLUDED.glossary, imported_at = now()
            """,
            (manifest.exercise_id, manifest.name, manifest.started_at_utc,
             manifest_file.sha256, glossary_file.sha256, Jsonb(glossary)),
        )
        conn.execute(
            "INSERT INTO pilot_import_archive (exercise_id, digest) "
            "SELECT %s, unnest(%s::text[]) ON CONFLICT DO NOTHING",
            (manifest.exercise_id, [manifest_file.sha256, glossary_file.sha256]),
        )
        conn.execute("DELETE FROM exercise_source WHERE exercise_id = %s", (manifest.exercise_id,))
        for position, item in enumerate(items, start=1):
            source_id, created = save_source(conn, manifest.exercise_id, item)
            new_count += created
            conn.execute(
                "INSERT INTO exercise_source (exercise_id, source_key, source_file_id, position) VALUES (%s, %s, %s, %s)",
                (manifest.exercise_id, item.source.source_key, source_id, position),
            )
    return ImportSummary(manifest.exercise_id, len(items), new_count,
                         sum(len(item.records) for item in items), sum(len(item.issues) for item in items))
