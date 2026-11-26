# Live context replay and pilot

## Run the automated replay

This replay uses fictional facilities data. It starts a temporary loopback service, uses a temporary archive, and writes only to the database you specify. It needs Python 3.13, the repository requirements, and PostgreSQL. It does not require a web login or a language model.

Run these commands from a fresh checkout on Windows.

```powershell
py -3.13 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
docker compose up -d --wait db
docker compose exec -T db createdb -U review_owner roxi_live_replay
$env:EXERCISE_REVIEW_DATABASE_URL = 'postgresql://review_owner:review-local-owner@127.0.0.1:55432/roxi_live_replay'
$env:EXERCISE_REVIEW_EDITOR_URL = 'postgresql://review_editor:review-local-editor@127.0.0.1:55432/roxi_live_replay'
$env:EXERCISE_REVIEW_READER_URL = 'postgresql://review_reader:review-local-reader@127.0.0.1:55432/roxi_live_replay'
.venv/Scripts/python.exe tools/check_live_context.py
```

Choose a new disposable database for each run. On Linux, use `python3.13 -m venv .venv`, `.venv/bin/python` and shell `export` for the three variables. The GitHub workflow runs the same script alongside the existing unit, review, history, and backup checks.

The [fictional replay](../fixtures/live-context/replay.json) has two chat streams, two radio streams, asset and site registers, and a simulated fleet feed. The test loads the [answer key](../fixtures/live-context/answer-key.json) separately. CI generates PCM silence and injects reference cue text. This isolates storage, timing, linking, and permission checks from model accuracy or downloads. The public repository contains the authored text and simulation code.

| Check | Expected replay result |
| --- | --- |
| Capture | A duplicate returns the original capture ID. Channels can reuse an upstream ID. Edits preserve time and sender, and out-of-order arrival preserves the highest available version. |
| Classification | A failed radio worker leaves the original pending. A new worker can finish it. A historical pin before processing still shows the pending gap. |
| Reconciliation | One van joins across its register, fleet position, radio call, and chat. A second identifier has two candidates. Both conflicting cage reports remain visible. |
| Review | A reviewer links the Bay 2 chat correction to the Bay 3 radio report and publishes the frozen communications snapshot. The reviewed answer cites both. |
| Integration | HTTP, the Python client, and MCP return the same scoped answer and audio locator. Revoking the radio grant blocks the saved link and removes the joint conflict from subsequent answers. |
| Evidence | Four full and ranged audio downloads match the expected bytes. Fourteen history citations and two reviewed citations resolve to archived sources. |
| Backup and retention | The separate backup drill restores a queued radio capture and its original WAV, then verifies that purging the source exercise removes its WAV. |

### Run local speech recognition

Generate the West Annex WAVs and cache the Faster Whisper `small.en` model. Run the optional speech check using a fresh replay database. [Demo audio generator](../README.md#quickstart)

```powershell
.venv/Scripts/python.exe tools/check_live_context.py --speech-recording fixtures/drill-01/input/facilities-radio.wav
```

It submits a 5.5-second excerpt that includes cue F02, runs the queued worker offline, and asserts that it recovered Bay 1 and its reported status. It opens the exact uploaded WAV and measures wall-clock time from local upload through the first successful history read, including model initialization. On November 26, 2026, a Ryzen 9 9900X ran this check in approximately 2.4 seconds with cached weights and CPU `small.en`. The transcript was “Facilities copies. Bay 1 is in service.” This was a single synthetic speech sample.

## Run a timed pilot

Use authorized civilian facilities or logistics material and a consenting reviewer. Choose two comparable sets of questions. Assign one set to their usual timeline or manual workflow and one to the host app with Roxi. Alternate the order across reviewers or use separate exercise variants so an answer learned in one condition cannot carry over to the next.

1. Prepare comparable questions on a cross-channel correction, an acknowledgment, a clock cutoff with contradictory reports, and an original audio or chat excerpt. Freeze the answer key outside ingestion and record each expected locator and its acceptable interval.
2. Start timing when each question is presented. Stop after the reviewer gives their answer and opens the cited sources. Record assistant response latency separately from the time the reviewer spent checking evidence.
3. Have the reviewer inspect a second set of automatic proposals, including ambiguous links and conflicting reports. Time from opening the queue to publishing, and record accepted, edited, rejected, and unresolved items.
4. For the connector timing check, record the end of each completed call, API receipt, transcript availability, and first successful API query. Use a common clock and summarize each channel's sample count and p50/p95. Record gaps such as incomplete audio and receipt delays.
5. Score critical names and status words before review, proposed and reviewed link correctness against the key, answer correctness, and whether each citation reaches the expected audio interval or chat line. Record missed or falsely linked reports as separate outcomes.

| Field | Record during the pilot |
| --- | --- |
| Session and environment | Anonymous reviewer key, condition and order, fixture version, app version, hardware, model, source coverage |
| Each answer | Question ID, time to answer, time to verified evidence, score, source IDs, observed citation errors |
| Each review session | Number of proposals, review and publish duration, edits, missed and false links, unresolved cases |
| Each audio source | Completed and dropped clips, event-to-receipt latency, receipt-to-available latency, interval checks and transcription misses |

Store any participant material and per-person results in the authorized local workspace. Summarize results without identifying information. The earlier guided fictional session recorded estimated display speed. This timed session needs its own measurements. [Earlier evaluation](PILOT-EVALUATION.md)

## Release candidate

The planned `v0.2.0` includes the integration guide and this repeatable fixture. Run the automated replay, the existing smoke tests and backup drill, and verify the published answer and source disclosure behavior before tagging. The stable-tag workflow reruns CI for the tagged commit and reads the matching changelog section. [Integration guide](INTEGRATING-ROXI.md) · [Release workflow](../CONTRIBUTING.md#cut-a-release)
