# Exercise Communications Review

A project for reviewing recorded exercise voice and chat. The first milestone is a synthetic dataset for testing timestamp alignment, evidence playback, cross-channel corrections, and answers about incomplete records.

## Run the local app

Use Docker Desktop with the Linux engine running and Python 3.13. From the repository root in PowerShell, run the following commands.

```powershell
docker compose up -d --wait db
py -3.13 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m review_app migrate
.venv/Scripts/python.exe -m review_app import fixtures/drill-01/input/exercise.json
.venv/Scripts/python.exe -m uvicorn review_app.app:app --host 127.0.0.1 --port 8000
```

Open [Exercise review](http://127.0.0.1:8000/). The radio and chat lanes share the same exercise clock. Click a radio marker or row to select the transmission, then use **Play clip**. **Source coverage** lists radio clips with unknown exercise time and chat lines that the import skipped.

The first drill import reports three source versions, 62 records, and one diagnostic. An identical retry reports zero new source versions. PostgreSQL data persists in the Compose volume, and the original input bytes are archived under the ignored `.local/archive` directory. Timeline and evidence reads use a read-only database role. Review writes use a separate role with access to draft tables and permission to insert published revisions. The Compose roles and passwords are for this synthetic local setup.

Stop the foreground app with **Ctrl+C**. `docker compose stop db` stops PostgreSQL while keeping the data. On restart, start the database and rerun the last command above.

## Review and publish

1. Open **Review queue** and select **Extract proposals** to start a new draft. Extraction uses the exercise glossary and rules for reports, acknowledgments, and corrections.
2. Expand an event to inspect its original source and proposed fields. Choose a subject or status, resolve a link if the evidence supports it, select a decision, and click **Save review**. Accept the earlier event before accepting a link to it. An ambiguous acknowledgment can stay unresolved.
3. Open a radio evidence link to play the archived clip or edit the draft transcript. Transcript text and structured event fields are edited separately. **Extract proposals** can add events from newly recognized transcript content while retaining saved event reviews.
4. Choose **Publish revision**. The published page records the current event decisions, links, edited transcripts, and input file versions. Use **Revisions** to reopen an older version and play or read its original evidence.

The queue retains unresolved timing and flags recent contradictory reports about the same subject. The queue displays the captured reports and their review decisions so a reviewer can judge what they support. The local app serves this synthetic drill on loopback.

## Query published revisions

Publish a draft from **Review queue**, then open the [HTTP OpenAPI docs](http://127.0.0.1:8000/docs). The API and MCP server serve the same published events, transcripts, and evidence. Resolve the latest revision once, then keep its numeric ID in each query.

Run this example from the repository root to check both interfaces. It starts a local MCP subprocess and opens each kind of original source available in the revision.

```powershell
.venv/Scripts/python.exe tools/check_published_parity.py --exercise drill-01
```

For a local stdio MCP client, register the following server. Use the project path on your machine if it differs from this checkout.

```json
{
  "mcpServers": {
    "exercise-review": {
      "command": "E:\\Projects\\exercise-communications-review\\.venv\\Scripts\\python.exe",
      "args": ["-m", "review_app.mcp_server"],
      "env": { "PYTHONPATH": "E:\\Projects\\exercise-communications-review" }
    }
  }
}
```

The tools are **get_exercise_revision**, **search_records**, **list_events**, and **get_evidence**. Event queries default to accepted events. Use `review_state=all` to include proposals and rejected decisions. Record search matches case-insensitive text in the published transcript or chat excerpt. Cursors are opaque and bound to the revision and filters.

For raw evidence, use the returned `original_path` relative to the HTTP app base. The radio `playback_path` adds the clip start and end offsets. The original recording supports byte ranges, and the chat result includes the exact original line and its line number. Evidence IDs from a changed transcript resolve only in revisions that contain that text.

The developer interfaces use the read-only PostgreSQL connection. The HTTP app is local and the MCP transport uses stdio. To restrict developer queries to specific exercises, set `EXERCISE_REVIEW_API_EXERCISES` to a comma-separated list of allowed exercise IDs in both processes. An unset list allows all local published exercises.

## Transcribe recordings and ask questions

The local worker generates timestamped WebVTT and writes an importable bundle. Use a separate exercise ID to compare an automatic transcript with a reviewed exercise.

```powershell
.venv/Scripts/python.exe -m review_app transcribe fixtures/drill-01/input/exercise.json .local/drill-01-asr-run --exercise-id drill-01-asr
.venv/Scripts/python.exe -m review_app import .local/drill-01-asr-run/exercise.json
```

The default model is Faster Whisper `small.en`. Model weights cache under `.local/models` on first use. Add `--offline` to require cached weights. Audio inference runs on the machine. Generated cues contain file offsets and text. Speaker labels can come from reviewed transcripts. [Faster Whisper documentation](https://github.com/SYSTRAN/faster-whisper)

Open **Assistant** after publishing a review revision. The assistant uses exact glossary aliases, timestamps, and cue IDs when it can, and uses local Ollama to route broader questions. It returns evidence links and a separate verdict produced by the read-only MCP verifier. Every question is evaluated against the selected revision.

For broader wording, run Ollama locally and load `qwen3.5:4b`. In one terminal set `$env:OLLAMA_NO_CLOUD = '1'` and run `ollama serve`. In another terminal run `ollama pull qwen3.5:4b` once. The assistant uses the local service at `127.0.0.1:11434`. [Ollama local mode](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features)

The [pilot evaluation](docs/PILOT-EVALUATION.md) records the transcription score, answer-key results, and commands to rerun them.

## West Annex dock flow drill

The setting and participants are fictional. Windows speech synthesis supplies the voices. The facilities track covers the full 15-minute drill with silences between radio transmissions. The 45-second secondary track deliberately has no known start time on the exercise clock. Both are mono, 16-bit PCM WAV at 16 kHz.

| Path | Contents |
| --- | --- |
| [Exercise manifest](fixtures/drill-01/input/exercise.json) | Source paths, timing anchors, and the exercise glossary. |
| [Facilities audio](fixtures/drill-01/input/facilities-radio.wav) and [WebVTT](fixtures/drill-01/input/facilities-radio.vtt) | Main recording with 24 timestamped speaker turns. |
| [Secondary audio](fixtures/drill-01/input/unaligned-radio.wav) and [WebVTT](fixtures/drill-01/input/unaligned-radio.vtt) | Three speaker turns indexed by time within the file. |
| [IRC-style export](fixtures/drill-01/input/dispatch.irc) | Elapsed timestamps, 35 valid lines, and one intentionally malformed timestamp. |
| [Answer key](fixtures/drill-01/reference/answer-key.md) and [structured key](fixtures/drill-01/reference/answer-key.json) | Eight scored questions with audio intervals and chat line references. |
| [Draft transcript fixture](fixtures/drill-01/reference/facilities-radio-draft.vtt) | Alternative transcript for testing a one-cue correction across two revisions. |

Import only `fixtures/drill-01/input`. For a quick manual walkthrough, seek to **02:04** in the main WAV, then read **02:29** and **02:40** in the chat export. The answer key covers the correction, acknowledgment, ambiguous reply, conflicting reports, and missing time anchor.

The audio files are checked in and ready to play. To regenerate them after editing the authored WebVTT scripts, run the following from the repository root in Windows PowerShell with Microsoft David Desktop and Microsoft Zira Desktop installed.

```powershell
powershell.exe -NoProfile -File tools/Build-DrillAudio.ps1
```

## Project plan

[Technical specification](docs/PROJECT-SPEC.md) · [Implementation plan](docs/IMPLEMENTATION-PLAN.md)
