import re

import numpy as np
from faster_whisper import decode_audio

from .radio_messages import inline_speed_correction, speed_readback

SAMPLE_RATE = 16_000
FRAME_SECONDS = 0.02
TURN_PAUSE = 0.9
SHORT_TURN_SECONDS = 2
RECEIPT_START = re.compile(r"^(?:copy|roger|acknowledged|affirmative|yes sir|yes ma'am)\b", re.I)
CALLSIGN_START = re.compile(r"^(?:[a-z]{2,} ){1,2}(?:[a-z]{0,3}\d{2,5}[a-z]{0,2})\b", re.I)
REPAIR_START = re.compile(r"^(?:correction|make that|i meant)\b", re.I)


def group_fragments(fragments) -> list[tuple[float, float, str]]:
    turns = []
    for start, end, text in fragments:
        text = " ".join(text.split())
        if not text:
            continue
        if turns:
            previous_start, previous_end, previous_text = turns[-1]
            gap = start - previous_end
            # A complete sentence followed by an address or a numeric readback can mark
            # a new transmission even when the decoder puts almost no silence between them.
            repeated = speed_readback(text)
            previous_repair = inline_speed_correction(previous_text) if repeated and gap < 0.20 else None
            numeric_reply = repeated is not None and (gap >= 0.20 or (previous_repair and repeated == previous_repair[1]))
            boundary = RECEIPT_START.match(text) or (
                previous_text.endswith((".", "?", "!")) and (CALLSIGN_START.match(text) or numeric_reply)
            )
            continuation = REPAIR_START.match(text) and (
                gap < 0.35 or inline_speed_correction(f"{previous_text} {text}") is not None
            )
            if 0 <= gap < TURN_PAUSE and end - previous_start <= 30 and (continuation or not boundary):
                turns[-1] = previous_start, max(previous_end, end), f"{previous_text} {text}"
                continue
        turns.append((start, end, text))
    return turns


def active_frames(waveform: np.ndarray, start: float, end: float) -> np.ndarray:
    frame_size = round(FRAME_SECONDS * SAMPLE_RATE)
    low = max(0, int(start / FRAME_SECONDS))
    high = min(len(waveform) // frame_size, int(end / FRAME_SECONDS) + 1)
    frames = waveform[low * frame_size:high * frame_size].reshape(-1, frame_size)
    if len(frames) < 3:
        return np.array([])
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    floor = float(np.percentile(rms, 20))
    active = rms >= max(0.002, 1.8 * floor)
    sustained = active[:-1] & active[1:]
    return (np.flatnonzero(sustained) + low) * FRAME_SECONDS


def activity_bounds(waveform: np.ndarray, start: float, end: float) -> tuple[float, float] | None:
    """Trim the decoder's leading and trailing static without altering the source samples."""
    activity = active_frames(waveform, start - 0.55, end + 0.55)
    activity = activity[(activity >= start - 0.12) & (activity <= end + 0.16)]
    if not activity.size:
        return None
    return max(0, float(activity[0]) - 0.08), min(len(waveform) / SAMPLE_RATE, float(activity[-1]) + 0.12)


def short_bursts(waveform: np.ndarray, start: float, end: float):
    if end - start < 0.5:
        return
    activity = active_frames(waveform, start + 0.10, end - 0.10)
    for frames in np.split(activity, np.flatnonzero(np.diff(activity) > 0.18) + 1):
        if len(frames) >= 4 and 0.08 <= frames[-1] - frames[0] <= SHORT_TURN_SECONDS:
            yield max(start, float(frames[0]) - 0.08), min(end, float(frames[-1]) + 0.12)


def short_readback(model, waveform: np.ndarray, start: float, end: float,
                   earlier: float, later: float, language: str) -> str:
    # Small shifts in surrounding silence help distinguish a quiet one-word reply from noise.
    midpoint = (start + end) / 2
    center = round(midpoint * 4) / 4
    windows = {(max(earlier, center - 1.5 + shift), min(later, center + 1.5 + shift))
               for shift in (-0.25, 0, 0.25)}
    hypotheses = []
    for first, last in sorted(windows):
        if first >= last:
            continue
        segments, _ = model.transcribe(
            waveform[round(first * SAMPLE_RATE):round(last * SAMPLE_RATE)],
            language=language, beam_size=5, vad_filter=False, no_speech_threshold=None,
            condition_on_previous_text=False, word_timestamps=True, temperature=0.0,
        )
        matching = [segment for segment in segments if segment.avg_logprob >= -1.0
                    and any(word.start + first < end and word.end + first > start for word in segment.words)]
        if matching:
            confidence = min(segment.avg_logprob for segment in matching)
            text = " ".join(segment.text.strip() for segment in matching)
            hypotheses.append((confidence, text))
    return max(hypotheses, default=(0, ""))[1]


def radio_cues(model, source, language: str) -> tuple[float, list[tuple[float, float, str]]]:
    waveform = decode_audio(str(source), sampling_rate=SAMPLE_RATE)
    duration = len(waveform) / SAMPLE_RATE
    segments, _ = model.transcribe(
        waveform, language=language, beam_size=5, vad_filter=False, no_speech_threshold=None,
        condition_on_previous_text=False, word_timestamps=True, temperature=0.0,
    )
    turns = group_fragments((segment.start, segment.end, segment.text) for segment in segments)
    cues = []
    for index, (start, end, text) in enumerate(turns):
        earlier = turns[index - 1][1] if index else 0.0
        later = turns[index + 1][0] if index + 1 < len(turns) else duration
        bounds = activity_bounds(waveform, start, end)
        is_short = end - start <= SHORT_TURN_SECONDS
        if is_short and bounds is None:
            continue
        first, last = bounds or (start, end)
        if speed_readback(text):
            # Leave room for a quiet leading digit when the word alignment starts late.
            first = min(first, start - 0.45)
            if cues:
                earlier = max(earlier, cues[-1][1])
        first, last = max(first, earlier), min(last, later)
        if last <= first:
            first, last = start, end
        if is_short:
            text = short_readback(model, waveform, first, last, earlier, later, language) or text
        cues.append((first, last, text))
    recovered = []
    previous = 0.0
    for start, end, _ in [*cues, (duration, duration, "")]:
        for first, last in short_bursts(waveform, previous, start):
            text = short_readback(model, waveform, first, last, previous, start, language)
            if text:
                recovered.append((first, last, text))
        previous = max(previous, end)
    return duration, sorted([*cues, *recovered])
