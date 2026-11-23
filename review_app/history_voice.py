import asyncio
import os
from functools import lru_cache
from io import BytesIO
from threading import Lock

from fastapi import HTTPException, Request

from .transcription import CACHE, DEFAULT_MODEL

SAMPLE_RATE = 16_000
MAX_SECONDS = 20
MAX_BYTES = 2 * 1024 * 1024
FORMATS = {"audio/webm": "matroska", "audio/ogg": "ogg", "audio/mp4": "mov", "audio/wav": "wav"}
_inference_lock = Lock()


@lru_cache(maxsize=1)
def voice_model():
    from faster_whisper import WhisperModel

    return WhisperModel(DEFAULT_MODEL, device="cpu", compute_type="int8",
                        cpu_threads=min(8, os.cpu_count() or 1),
                        download_root=str(CACHE), local_files_only=True)


def transcribe_question(recording: bytes, audio_format: str) -> str:
    import av
    import numpy as np

    chunks = []
    samples = 0
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

    def take(frames):
        nonlocal samples
        for frame in frames:
            samples += frame.samples
            if samples > SAMPLE_RATE * MAX_SECONDS:
                raise HTTPException(413, f"Keep recordings under {MAX_SECONDS} seconds.")
            chunks.append(frame.to_ndarray().ravel())

    try:
        with av.open(BytesIO(recording), options={"protocol_whitelist": "pipe"}, format=audio_format) as audio:
            if not audio.streams.audio:
                raise HTTPException(422, "The recording has no audio stream.")
            for frame in audio.decode(audio=0):
                take(resampler.resample(frame))
            take(resampler.resample(None))
    except (av.FFmpegError, ValueError) as error:
        raise HTTPException(422, "The recording could not be decoded.") from error
    if samples == 0:
        raise HTTPException(422, "The recording is empty.")

    waveform = np.concatenate(chunks).astype(np.float32) / 32768
    with _inference_lock:
        try:
            model = voice_model()
        except (OSError, RuntimeError, ValueError) as error:
            raise HTTPException(503, "The local speech model is unavailable. See the voice setup instructions.") from error
        segments, _ = model.transcribe(waveform, language="en", beam_size=3, vad_filter=True,
                                       condition_on_previous_text=False)
        question = " ".join(segment.text.strip() for segment in segments).strip()
    if not question:
        raise HTTPException(422, "No speech was recognized. Try again or type your question.")
    return question[:1000]


async def receive_question(request: Request) -> str:
    origin = f"{request.url.scheme}://{request.url.netloc}"
    if (request.client is None or request.client.host not in {"127.0.0.1", "::1"}
            or request.url.hostname not in {"127.0.0.1", "::1", "localhost"}
            or request.headers.get("origin", origin) != origin):
        raise HTTPException(403, "Voice is available on the local Roxi app.")
    mime = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if mime not in FORMATS:
        raise HTTPException(415, "Record audio in WebM, Ogg, MP4, or WAV format.")
    recording = bytearray()
    async for chunk in request.stream():
        if len(recording) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "The recording is too large.")
        recording.extend(chunk)
    return await asyncio.to_thread(transcribe_question, bytes(recording), FORMATS[mime])
