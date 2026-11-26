"""Freeze scoped channel evidence into the existing immutable review workflow."""

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory

from .channel_capture import audio_url
from .importer import Manifest
from .source_catalog import capture_url
from .storage import READER_DSN, archive_lock, archive_path, connect_read
from .transcription import timestamp


def archive_audio(digest: str) -> bytes:
    data = archive_path(digest).read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("An original radio clip failed the archive digest check.")
    return data


def channel_rows(conn, exercise_id: str, known_at: datetime) -> list[dict]:
    return conn.execute(
        """
        SELECT latest.* FROM (
            SELECT DISTINCT ON (item.source_key, item.item_key)
                   item.*, original.captured_at, derived.records
            FROM channel_capture item JOIN catalog_capture original ON original.id = item.capture_id
            LEFT JOIN catalog_derivation derived ON derived.capture_id = item.capture_id
                AND derived.parser_version = 1 AND derived.derived_at <= %(known)s
            WHERE item.exercise_id = %(exercise)s AND original.captured_at <= %(known)s
            ORDER BY item.source_key, item.item_key, item.item_version DESC
        ) AS latest
        ORDER BY observed_at, source_key, capture_id
        """,
        {"exercise": exercise_id, "known": known_at},
    ).fetchall()


def export_channels(exercise_id: str, review_id: str, output: Path, *, known_at: datetime) -> dict:
    if review_id == exercise_id or output.exists():
        raise ValueError("Choose a separate exercise ID and a new output directory.")
    if known_at > datetime.now(UTC):
        raise ValueError("The known-by cutoff must be in the past or present.")
    with archive_lock(shared=True, dsn=READER_DSN), connect_read() as conn:
        exercise = conn.execute("SELECT name, glossary, started_at_utc FROM exercise WHERE id = %s",
                                (exercise_id,)).fetchone()
        if not exercise or exercise["started_at_utc"] is None:
            raise ValueError("The exercise must have a UTC start time before exporting.")
        rows = channel_rows(conn, exercise_id, known_at)
        return write_bundle(exercise, rows, exercise_id, review_id, output, known_at)


def write_bundle(exercise, rows, exercise_id, review_id, output, known_at):
    omitted = [row["capture_id"] for row in rows if not row["records"]]
    origin = exercise["started_at_utc"]
    chats = defaultdict(list)
    locations, sources = [], []
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".channel-export-", dir=output.parent) as temporary:
        bundle = Path(temporary) / "bundle"
        bundle.mkdir()
        for row in rows:
            if not row["records"]:
                continue
            offset = round((row["observed_at"] - origin).total_seconds() * 1000)
            if offset < 0:
                raise ValueError("One capture precedes the exercise start time.")
            source_id, capture_id = row["source_key"], row["capture_id"]
            if row["audio_digest"]:
                source = f"clip-{capture_id}"
                wav, vtt = f"{source}.wav", f"{source}.vtt"
                (bundle / wav).write_bytes(archive_audio(row["audio_digest"]))
                cues = []
                for item in row["records"]:
                    clip, data = item["locator"], item["data"]
                    cues.append(f"{clip['value']}\n{timestamp(clip['start_ms'])} --> {timestamp(clip['end_ms'])}\n"
                                f"<v {row['sender']}>{escape(data['text'], quote=False)}")
                (bundle / vtt).write_text("WEBVTT\n\n" + "\n\n".join(cues) + "\n", encoding="utf-8")
                sources.append({"source_key": source, "kind": "audio", "file": wav, "transcript_file": vtt,
                                "exercise_offset_ms": offset})
                locations.append({"review_source": source, "channel": source_id, "capture_id": capture_id,
                                  "original_path": audio_url(exercise_id, source_id, capture_id)})
            else:
                chats[source_id].append((timestamp(offset), row, row["records"][0]["data"]["text"]))
        for source_id, messages in sorted(chats.items()):
            source = f"chat-{messages[0][1]['capture_id']}"
            file = f"{source}.irc"
            lines = []
            for number, (sent, row, body) in enumerate(messages, 1):
                lines.append(f"[{sent}] <{row['sender']}> {body}\n")
                locations.append({"review_source": source, "line": number, "channel": source_id,
                                  "capture_id": row["capture_id"],
                                  "original_path": capture_url(exercise_id, source_id, row["capture_id"]) + "/original"})
            (bundle / file).write_text("".join(lines), encoding="utf-8", newline="\n")
            sources.append({"source_key": source, "kind": "chat", "file": file, "time_basis": "exercise_elapsed_ms"})
        if not sources:
            raise ValueError("Classify at least one usable channel capture before exporting.")
        manifest = Manifest.model_validate({"exercise_id": review_id, "name": exercise["name"][:139] + " (channel review)",
                                            "glossary_file": "glossary.json", "started_at_utc": origin, "sources": sources})
        (bundle / "exercise.json").write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        metadata = {"exercise_id": exercise_id, "known_at": known_at.isoformat(),
                    "excluded_capture_ids": omitted, "sources": locations}
        for file, value in (("glossary.json", exercise["glossary"]), ("source-map.json", metadata)):
            (bundle / file).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        bundle.rename(output)
    return {"sources": len(sources), "excluded_capture_ids": omitted}
