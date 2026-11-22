# West Annex Drill Answer Key

This is the scoring reference for the fictional 15-minute West Annex dock flow drill. Keep this directory outside ingestion and agent retrieval. Use the [exercise manifest](../input/exercise.json) as the import entry point. The [machine-readable key](answer-key.json) carries the same expected outcomes and exact source locators.

## Scored questions

Exercise times are elapsed from the start of the drill. Audio citations list offsets into the locally generated WAV files. [Generate the recordings](../../../README.md#quickstart)

| Question | Expected answer | Source |
| --- | --- | --- |
| At 02:28.999, which bay had been reported out of service? | Bay 3 was reported out of service. This is the original radio report as it stood immediately before the chat correction. | `facilities-radio.wav` `124.000–126.600 s` ([cue F06](../input/facilities-radio.vtt)) |
| At 02:29.000, which bay did the out-of-service report apply to, and what changed? | The chat correction changes the subject from Bay 3 to Bay 2. The corrected report is that Bay 2 was out of service. Bay 3's actual condition remains unknown from these messages. | `facilities-radio.wav` `124.000–126.600 s` ([cue F06](../input/facilities-radio.vtt))<br>[chat line 7](../input/dispatch.irc#L7) |
| Who acknowledged the Bay 2 correction, and what did they acknowledge? | Dispatch acknowledged the Bay 2 out-of-service correction at 02:40. The acknowledgment establishes receipt. | [chat line 7](../input/dispatch.irc#L7)<br>[chat line 8](../input/dispatch.irc#L8) |
| Which report does the 05:48 Copy refer to? | The referent is unresolved. Cart C-14 going out of service and Lift L-7 returning to service were both reported at 05:40. The reply supplies neither subject. | [chat line 14](../input/dispatch.irc#L14)<br>[chat line 15](../input/dispatch.irc#L15)<br>[chat line 16](../input/dispatch.irc#L16) |
| At 08:00, was Returns Cage 1 open for returns? | Facilities reported that Returns Cage 1 was open for returns over radio. Receiving reported it closed in chat. The record contains conflicting reports with neither identified as a correction. | `facilities-radio.wav` `455.000–458.650 s` ([cue F14](../input/facilities-radio.vtt))<br>[chat line 20](../input/dispatch.irc#L20) |
| Was Bay 2 confirmed back in service by 12:30? | Bay 2's return to service is unconfirmed by 12:30. The last aligned status report says it was out of service. The secondary radio contains a return-to-service report with unknown exercise time. | `facilities-radio.wav` `124.000–126.600 s` ([cue F06](../input/facilities-radio.vtt))<br>[chat line 7](../input/dispatch.irc#L7)<br>`unaligned-radio.wav` `15.000–18.750 s` ([cue U02](../input/unaligned-radio.vtt)) |
| Where is the untimed Bay 2 return-to-service report? | The report is in the secondary recording. It is playable at the cited file offset. Its exercise time is unknown. | `unaligned-radio.wav` `15.000–18.750 s` ([cue U02](../input/unaligned-radio.vtt)) |
| Which cart was reported staged beside Bay 1 in cue F18? | Yard reported Cart C-17 staged beside Bay 1. The draft fixture deliberately masks the cart label in the same clip. | `facilities-radio.wav` `642.000–647.150 s` ([cue F18](../input/facilities-radio.vtt)) |

## Review and import checks

- Import all inputs under the manifest. Expect 24 cues in the 15-minute facilities recording, three cues in the 45-second secondary recording, and 35 valid chat lines. Expect a source diagnostic for the deliberately malformed timestamp on [chat line 24](../input/dispatch.irc#L24).
- Preserve [chat lines 14 and 15](../input/dispatch.irc#L14) as separate records at 05:40, in file order. The ambiguous “Copy” on line 16 can suggest either candidate and should remain unresolved.
- Open the secondary audio using its file offsets. Strict time-cutoff queries exclude its report. The equality case at 02:29.000 includes the aligned chat correction.
- Import the same exercise and profile twice. Expect a single visible set of sources and records.
- To test revision history, load [the draft transcript](facilities-radio-draft.vtt) as the initial transcript for the facilities track in a disposable test import or apply its one-cue edit in the reviewer. Publish that draft, then restore the reference wording for cue F18 using the original WAV and publish again. Both revisions should cite the same audio file interval and return their respective transcript text.
- To test concurrent edits, open the same draft in two reviewer sessions. Save one F18 edit, then attempt to save the other from the earlier draft version. The second save should report a conflict and let the reviewer reconcile.

## Manual benchmark

Give each reviewer the questions without the answers. Compare the synchronized sources with the structured view using different orders or equivalent questions. Record seconds to a correct answer, citations opened, incorrect links, and missed corrections. Score the ambiguous acknowledgment, independent contradiction, and unknown-time report on whether the reviewer preserves the uncertainty.
