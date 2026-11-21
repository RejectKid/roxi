# Exercise Communications Review

A project for reviewing recorded exercise voice and chat. The first milestone is a synthetic dataset for testing timestamp alignment, evidence playback, cross-channel corrections, and answers about incomplete records.

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
