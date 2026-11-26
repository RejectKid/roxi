"""Capture and transcribe completed communications on registered civilian channels."""

import base64
import binascii
import hashlib
import io
import wave
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, field_validator

from . import source_catalog as store
from .extraction import Extractor, Proposal
from .importer import Slug, parse_vtt
from .live_chat import connect_editor
from .query_models import Input
from .source_adapters import (
    ADAPTERS, CHANNEL_CHAT, CHANNEL_RADIO, ChannelAudioLocator, Issue, Locator, Parsed, Record,
)
from .storage import ARCHIVE_LOCK, archive_capture, archive_path, archive_writer, connect_read

WAV_LIMIT = 1_400_000
ENCODED_LIMIT = (WAV_LIMIT + 2) // 3 * 4
CLIP_LIMIT_MS = 30_000
VERSION = 1


class ChannelItem(Input):
    observed_at: AwareDatetime
    sender: Slug

    @field_validator("observed_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class ChannelMessage(ChannelItem):
    wire_schema: Literal["roxi.channel-chat/v1"] = Field(alias="schema")
    message_id: Slug
    version: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
    text: Annotated[str, Field(min_length=1, max_length=2000)]

    @field_validator("text")
    @classmethod
    def single_line(cls, value: str) -> str:
        if not value.strip() or value.splitlines() != [value] or "\x00" in value:
            raise ValueError("Use a nonblank, single-line message.")
        return value


class CompletedClip(ChannelItem):
    wire_schema: Literal["roxi.completed-radio/v1"] = Field(alias="schema")
    clip_id: Slug
    wav_base64: Annotated[str, Field(min_length=1, max_length=ENCODED_LIMIT)]


CAPTURE = TypeAdapter(Annotated[ChannelMessage | CompletedClip, Field(discriminator="wire_schema")])


@dataclass(frozen=True)
class ChannelReceipt:
    capture_id: int
    state: Literal["pending", "accepted", "partial", "quarantined"]
    duplicate: bool = False


def wave_input(encoded: str) -> tuple[bytes, int]:
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > WAV_LIMIT:
            raise ValueError
        with wave.open(io.BytesIO(raw), "rb") as reader:
            frames, rate = reader.getnframes(), reader.getframerate()
            channels = reader.getnchannels()
            if channels not in {1, 2} or reader.getsampwidth() != 2 or not 8_000 <= rate <= 48_000:
                raise ValueError
            duration = round(1000 * frames / rate)
            if not 0 < duration <= CLIP_LIMIT_MS or len(reader.readframes(frames)) != frames * 2 * channels:
                raise ValueError
    except (ValueError, binascii.Error, wave.Error, EOFError):
        raise store.CatalogError(422, "Supply a complete 16-bit PCM WAV clip up to 30 seconds and 1.4 MB.") from None
    return raw, duration


def select_channel(conn, exercise_id: str, source_key: str, capture_id: int) -> dict:
    row = conn.execute(
        """
        SELECT item.*, original.digest, original.captured_at, derivation.state
        FROM channel_capture item JOIN catalog_capture original ON original.id = item.capture_id
        LEFT JOIN catalog_derivation derivation ON derivation.capture_id = item.capture_id
            AND derivation.parser_version = 1
        WHERE item.exercise_id = %s AND item.source_key = %s AND item.capture_id = %s
        """,
        (exercise_id, source_key, capture_id),
    ).fetchone()
    if row is None:
        raise store.CatalogError(404, "Channel capture not found.")
    return row


def speech_record(extractor: Extractor, key, kind, observed_at, sender, text, locator, **timeline) -> Record:
    proposal = extractor.extract(text) or Proposal("chatter", None, None)
    data = {"sender": sender, "text": text, "medium": kind, "reported_status": proposal.reported_status,
            "reported_time_seconds": proposal.reported_time_seconds, **timeline}
    return Record(key, proposal.subject_id, proposal.kind, observed_at, data, locator)


def publish_derivation(conn, item: dict, adapter_key: str, parsed: Parsed) -> dict:
    result = store.save_derivation(conn, item["capture_id"], ADAPTERS[adapter_key], VERSION, parsed)
    conn.execute(
        """
        INSERT INTO catalog_attempt (exercise_id, source_key, channel, capture_id, parser_version)
        VALUES (%s, %s, 'file', %s, %s)
        """,
        (item["exercise_id"], item["source_key"], item["capture_id"], VERSION),
    )
    return result


@archive_writer
def receive(exercise_id: str, source_key: str, item: ChannelMessage | CompletedClip, original: bytes) -> ChannelReceipt:
    is_audio = isinstance(item, CompletedClip)
    adapter = CHANNEL_RADIO if is_audio else CHANNEL_CHAT
    key, version = (item.clip_id, VERSION) if is_audio else (item.message_id, item.version)
    payload_sha = hashlib.sha256(item.model_dump_json().encode()).hexdigest()
    raw, duration = wave_input(item.wav_base64) if is_audio else (None, None)

    with connect_editor() as conn:
        source = store.registered(conn, exercise_id, source_key, lock=True)
        if source["adapter_key"] != adapter:
            raise store.CatalogError(422, "The capture format does not match this channel.")
        previous = conn.execute(
            """
            SELECT item.*, derivation.state
            FROM channel_capture item LEFT JOIN catalog_derivation derivation
              ON derivation.capture_id = item.capture_id AND derivation.parser_version = 1
            WHERE item.exercise_id = %s AND item.source_key = %s AND item.item_key = %s
            ORDER BY (item.item_version = %s) DESC, item.item_version DESC LIMIT 1
            """,
            (exercise_id, source_key, key, version),
        ).fetchone()
        if previous and previous["item_version"] == version:
            if previous["payload_sha256"] != payload_sha:
                raise store.CatalogError(409, "That channel version already has different content.")
            return ChannelReceipt(previous["capture_id"], previous["state"] or "pending", True)
        if previous and (previous["observed_at"], previous["sender"]) != (item.observed_at, item.sender):
            raise store.CatalogError(409, "An edit must preserve the original time and sender.")

        envelope = archive_capture(original)
        audio_digest = archive_capture(raw).sha256 if raw is not None else None
        capture_id = conn.execute(
            """
            INSERT INTO catalog_capture (exercise_id, source_key, digest, byte_size, original_name)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
            """,
            (exercise_id, source_key, envelope.sha256, envelope.byte_size, "channel-capture.json"),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO channel_capture (capture_id, exercise_id, source_key, item_key, item_version,
                                         observed_at, sender, payload_sha256, audio_digest, duration_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (capture_id, exercise_id, source_key, key, version, item.observed_at, item.sender,
             payload_sha, audio_digest, duration),
        )
        if is_audio:
            return ChannelReceipt(capture_id, "pending")
        glossary = conn.execute("SELECT glossary FROM exercise WHERE id = %s", (exercise_id,)).fetchone()["glossary"]
        record = speech_record(Extractor(glossary, elapsed_times=True), key, "chat", item.observed_at,
                               item.sender, item.text, Locator("json_pointer", ""),
                               message_id=key, version=version)
        saved = publish_derivation(conn, {"exercise_id": exercise_id, "source_key": source_key,
                                         "capture_id": capture_id}, adapter, Parsed([record], []))
        return ChannelReceipt(capture_id, saved["state"])


def radio_batch(conn, transcribe, *, model_name: str | None = None, exercise_id: str | None = None) -> bool:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (ARCHIVE_LOCK,))
        job = conn.execute(
            """
            SELECT item.*, exercise.glossary
            FROM channel_capture item JOIN exercise ON exercise.id = item.exercise_id
            WHERE audio_digest IS NOT NULL
              AND (%s::text IS NULL OR item.exercise_id = %s)
              AND NOT EXISTS (SELECT 1 FROM catalog_derivation d WHERE d.capture_id = item.capture_id)
            ORDER BY item.capture_id LIMIT 1 FOR UPDATE OF item SKIP LOCKED
            """, (exercise_id, exercise_id),
        ).fetchone()
        if job is None:
            return False
        if conn.execute("SELECT 1 FROM catalog_derivation WHERE capture_id = %s", (job["capture_id"],)).fetchone():
            return True
        audio = archive_path(job["audio_digest"])
        try:
            raw = audio.read_bytes()
        except OSError:
            raw = b""
        if hashlib.sha256(raw).hexdigest() != job["audio_digest"]:
            parsed = Parsed([], [Issue("source_unavailable", "The original radio clip failed verification.",
                                      Locator("json_pointer", ""))])
        else:
            # Real workers use cached weights. CI can inject a fixture transcript without a model download.
            with TemporaryDirectory(prefix="roxi-radio-") as folder:
                path = Path(folder) / "clip.wav"
                path.write_bytes(raw)
                vtt = transcribe(path)
            extractor = Extractor(job["glossary"], elapsed_times=True)
            try:
                cues = parse_vtt(vtt, job["duration_ms"], 0)
            except ValueError:
                parsed = Parsed([], [Issue("invalid_transcript", "The generated transcript has invalid clip timing.",
                                          Locator("json_pointer", ""))])
                publish_derivation(conn, job, CHANNEL_RADIO, parsed)
                return True
            records = [
                speech_record(extractor, f"{job['item_key']}:{cue.ordinal}", "radio",
                              job["observed_at"] + timedelta(milliseconds=cue.audio_start_ms),
                              job["sender"], cue.body,
                              ChannelAudioLocator(cue.cue_id or str(cue.ordinal), cue.audio_start_ms, cue.audio_end_ms),
                              clip_id=job["item_key"], clip_started_at=job["observed_at"].isoformat(),
                              transcription_model=model_name)
                for cue in cues
            ]
            issues = [] if records else [Issue("no_transcript", "The clip produced no transcript.",
                                              Locator("json_pointer", ""))]
            parsed = Parsed(records, issues)
        publish_derivation(conn, job, CHANNEL_RADIO, parsed)
        return True


def work_radio(model_name: str, *, once: bool = False, offline: bool = False, exercise_id: str | None = None) -> int:
    import os

    from faster_whisper import WhisperModel

    from .transcription import CACHE, transcribe_recording

    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=min(8, os.cpu_count() or 1),
                         download_root=str(CACHE), local_files_only=offline)

    def transcribe(path):
        text, _ = transcribe_recording(model, path, "en", profile="radio")
        return text

    with connect_editor(autocommit=True) as conn:
        while True:
            processed = radio_batch(conn, transcribe, model_name=model_name, exercise_id=exercise_id)
            if once:
                return int(processed)
            if not processed:
                sleep(0.2)


def audio_url(exercise_id: str, source_key: str, capture_id: int) -> str:
    return f"/v1/exercises/{exercise_id}/channels/{source_key}/captures/{capture_id}/audio"


def lookup(exercise_id: str, source_key: str, capture_id: int) -> dict:
    with connect_read() as conn:
        return select_channel(conn, exercise_id, source_key, capture_id)
