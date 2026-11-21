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

The first drill import reports three source versions, 62 records, and one diagnostic. An identical retry reports zero new source versions. PostgreSQL data persists in the Compose volume, and the original input bytes are archived under the ignored `.local/archive` directory. The web app uses a read-only database role. The Compose roles and passwords are for this synthetic local setup.

Stop the foreground app with **Ctrl+C**. `docker compose stop db` stops PostgreSQL while keeping the data. On restart, start the database and rerun the last command above.

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
