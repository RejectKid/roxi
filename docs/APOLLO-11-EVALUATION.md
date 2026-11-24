# Apollo 11 Flight Day 1 replay benchmark

Milestone 14 began with 51 seconds of NASA's Flight Day 1 highlights, from **05:32.000 to 06:23.000** in the MP3. The four reference turns are the original time report, the crew's readback, Houston's correction, and the crew's acknowledgment. The announced time changed from **03:15:00 to 03:15:03** mission elapsed time. [NASA Flight Day 1 audio and companion transcript](https://www.nasa.gov/history/apollo-11-audio-highlights/) · [NASA technical air-to-ground transcript](https://www.nasa.gov/wp-content/uploads/static/history/alsj/a11/a11transcript_tec.html)

## Results

| Check | Automatic transcript | NASA transcript control |
| --- | ---: | ---: |
| Transcription word error rate | **55.6%** · 40 edits over 72 reference words | Reference input |
| Announced separation times recovered | 1 of 2 | 2 of 2 |
| Correct correction-to-report links suggested | 0 of 1 | 0 of 1 |
| All expected links suggested | 0 of 3 | 0 of 3 |
| Published audio URLs served the cited bytes | 4 of 4 | 4 of 4 |
| Published URLs played the listed ranges in Chrome | 4 of 4 | 4 of 4 |
| Reference turns that had a matching single citation | 0 of 4 | 4 of 4 |
| Assistant verdict for the corrected time | Insufficient evidence, zero citations | Insufficient evidence, zero citations |

The automatic transcript recovered the original announced time and spread the report over three cues. It captured only the tail of the correction and missed the readback and final acknowledgment. With the NASA reference text, the extractor still treated the correction as an acknowledgment. The turn starts with a receipt word and expresses the correction through a revised number and an admission of error. Roxi's existing rules recognize explicit "correction" phrasing and a small set of service-status reports.

## How it was scored

The test decoded the NASA excerpt to 16 kHz mono PCM, then used Roxi's existing Faster Whisper `small.en` configuration on CPU INT8. Each input went through import, authenticated review extraction, and publication in a fresh database. The automatic transcript went through without edits. The reference control used the same WAV with NASA's original timed text and speaker labels. Word error rate uses case-insensitive word edit distance with punctuation removed. Split numerals count as word differences. The announced times are scored separately by comparing their digits.

The gold intervals below are file-relative offsets in the **51-second excerpt**, aligned from NASA's timed transcript and the highlights waveform. Mission timestamps identify the four transcript turns. The highlights MP3 uses its own playback clock. [NASA air-to-ground transcript, tape 2/9, page 17](https://www.nasa.gov/wp-content/uploads/static/history/alsj/a11/a11transcript_tec.html)

| Turn | NASA mission time | Excerpt interval |
| --- | --- | --- |
| Original report | 02:54:09 | 00:01.420–00:19.230 |
| Crew readback | 02:54:33 | 00:25.420–00:37.940 |
| Correction | 02:54:46 | 00:38.510–00:43.080 |
| Crew acknowledgment | 02:54:55 | 00:47.750–00:48.260 |

The clip scorer verified the archived WAV digest, the two playback fragment timestamps, and the exact bytes returned by HTTP range requests. Chrome then played every published URL from its specified start and paused within 350 ms of the specified end. A citation counted as matching a reference turn if it started within 500 ms of the reference, ended within 1.5 seconds, and covered at least 60% of the reference interval. These are the existing West Annex citation tolerances.

This replay measures transcription, extraction, and evidence playback on one recorded incident. A participant is still needed to measure speed and reviewer workload against a real team's current workflow.

## Repeat the evaluation

Use the Python and PostgreSQL setup in the [quickstart](../README.md#quickstart) and the cached `small.en` model from the [speech-to-text setup](PILOT-EVALUATION.md#repeat-the-checks). Run from the repo root with a fresh database and output directory. The evaluator downloads and verifies the pinned NASA inputs, then saves the excerpt, generated transcript, archive, and JSON report under the ignored local output directory.

```powershell
docker compose exec -T db createdb -U review_owner roxi_apollo_check
$env:EXERCISE_REVIEW_DATABASE_URL = 'postgresql://review_owner:review-local-owner@127.0.0.1:55432/roxi_apollo_check'
$env:EXERCISE_REVIEW_EDITOR_URL = 'postgresql://review_editor:review-local-editor@127.0.0.1:55432/roxi_apollo_check'
$env:EXERCISE_REVIEW_READER_URL = 'postgresql://review_reader:review-local-reader@127.0.0.1:55432/roxi_apollo_check'
.venv/Scripts/python.exe tools/evaluate_apollo11.py --output .local/apollo11-day1/recheck
```

To repeat the browser check, add `--browser-check tools/check_apollo_playback.cjs`. The check uses Playwright with Chromium. Set `ROXI_PLAYWRIGHT_MODULE` to the absolute path of your installed Playwright package if it is outside the checkout, and optionally set `ROXI_BROWSER_EXECUTABLE` to a local Chrome binary.

NASA recordings and transcript files are downloaded into the local cache. Credit NASA when using its source material. [NASA media usage guidelines](https://www.nasa.gov/nasa-brand-center/images-and-media/)
