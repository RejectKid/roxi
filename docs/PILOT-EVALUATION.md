# West Annex Pilot Evaluation

## Local speech to text

Faster Whisper ran the `small.en` model on the two original WAV files using CPU INT8, voice activity detection, and word timestamps. It generated 41 WebVTT cues. The import pipeline loaded those cues and the 35 valid chat lines as a separate exercise in the test database. The generated bundle retained the bytes of each original audio and chat file.

| Recording | Reference words | Word edits | Word error rate | Critical terms recovered |
| --- | ---: | ---: | ---: | ---: |
| Facilities radio | 241 | 4 | 1.66% | 11 of 11 |
| Secondary radio | 30 | 1 | 3.33% | 2 of 2 |
| Total | 271 | 5 | 1.85% | 13 of 13 |

Word error rate used case-insensitive token edit distance with punctuation removed. All five differences were written number forms. The reference spelled out `one`, `seven`, and `two` where the engine returned digits. The critical-term check counted glossary subject aliases and service status expressions in overlapping cues, with a 500 millisecond tolerance at each reference cue boundary. The two tracks took 11.25 seconds of transcription after model load. These measurements apply to the clean synthetic West Annex recordings.

## Answers and original evidence

The assistant answered the eight questions in the drill answer key using the reviewed reference transcript and accepted events in test revision 2. The independent verifier opened the citations over read-only MCP and compared the structured claims with the published event fields and source excerpts. The scoring run checked the answer facts, exact set of primary and coverage citations, WAV byte-range access, and the cited original chat line. Each of the eight answers and citation sets matched. Each claim was supported by the independent check.

| Case | Result |
| --- | --- |
| First radio report | The reply cited Bay 3 at the cutoff immediately before the correction. |
| Cross-channel correction | The reply identified Bay 2 and cited the original radio report and chat line 7. |
| Explicit acknowledgment | The reply identified Dispatch and cited chat lines 7 and 8. |
| Ambiguous acknowledgment | The reply left the link unresolved and cited the two possible reports and the acknowledgment. |
| Conflicting reports | The reply surfaced both versions of the Returns Cage 1 report. |
| Insufficient timed evidence | The reply reported the last aligned condition and identified the unaligned clip as coverage evidence. |
| File-relative playback | The reply linked the untimed Bay 2 report at 15 seconds into the secondary recording. |
| Amended equipment label | The reply identified Cart C-17 with cue F18. Asking on revision 1 returned the unclear label from that version. |

Two negative controls exercised the verifier. Substituting the wrong bay returned `contradicted`. Supplying an invalid evidence ID returned `insufficient_evidence`. The eight answer-key questions exercised the structured parsing path. A separate paraphrase, “Who read back the Bay 2 message?”, used the local Qwen model and returned the verified Dispatch acknowledgment.

The evaluation scores transcription and question answering independently. The answer scores measure retrieval and reasoning over a reviewed reference revision. The transcription results measure raw audio recognition before review.

## End-to-end run on reviewed automatic transcripts

Milestone 6 imported the 41 generated cues, then used the original WAV waveforms to consolidate adjacent fragments into 27 transmission cues. The [review map](../fixtures/drill-01/reference/asr-review.tsv) records the source cue IDs, measured speech spans, and drill labels. The reviewer moved the stray “Facilities” call sign from the end of the Bay 3 transmission to the start of the following transmission. Cue IDs and speaker labels use the fixture's known annotations. The reviewed text comes from the generated words, with casing and punctuation edits on F06 and F18. The original WAVs and IRC export match the fixture byte for byte.

The review accepted 18 events. Chat line 7 corrects radio cue F06, and chat line 8 acknowledges line 7. Chat line 16 remains unresolved between the two messages at 05:40. The two Returns Cage 1 reports remain in conflict, and the secondary track remains untimed on the exercise clock. The reviewed inputs, events, and edits were published as `drill-01-asr` revision 1.

| Check | Result |
| --- | --- |
| Live assistant answers against the drill key | 8 of 8 |
| Expected evidence sets and original media or chat lines | 8 of 8 |
| Independent MCP verdicts | 8 of 8 supported |
| Wrong subject and invalid citation controls | Contradicted and insufficient evidence, respectively |
| Published HTTP and MCP responses | Matched, including original audio and chat |

The audio citation check requires the expected file hash, speaker, cue, and normalized words. It accepts reviewed windows within 500 ms of the reference start and 1,500 ms of the reference end, covering at least 60% of the reference window. The evaluator checks the playback fragment and compares the actual served byte range with the fixture at those offsets. Chat citations require exact line numbers and text. The untimed Bay 2 report opens at 15.030 seconds into the secondary WAV.

The desktop Chrome check opened the correction's original chat line and started the radio clip at 124.030 seconds. A separate paraphrase passed through local Ollama and returned the verified Dispatch acknowledgment. These results describe this single synthetic exercise.

## Repeat the checks

Run the following from the repository root on a fresh local database or with a new exercise ID and fresh output directories. The cue map applies to the captured `small.en` segmentation from the drill and requires review if a new model run changes those fragments.

```powershell
.venv/Scripts/python.exe -m review_app transcribe fixtures/drill-01/input/exercise.json .local/drill-01-asr-rerun --exercise-id drill-01-asr
.venv/Scripts/python.exe -m review_app import .local/drill-01-asr-rerun/exercise.json
.venv/Scripts/python.exe tools/prepare_reviewed_drill.py .local/drill-01-asr-rerun .local/drill-01-reviewed-asr-rerun
.venv/Scripts/python.exe -m review_app import .local/drill-01-reviewed-asr-rerun/exercise.json
```

Open the reviewed exercise, extract proposals, and accept the reports and acknowledgments with the links above. Leave line 16 unresolved. In the F06 transcript editor, save “Bay three is OOS.” In F18, save “Dispatch, Yard. Cart C-17 is staged beside Bay one.” Publish, then give the evaluator the exercise ID and published revision number.

```powershell
.venv/Scripts/python.exe tools/evaluate_drill.py --asr-bundle .local/drill-01-asr-rerun --exercise drill-01-asr --revision 1 --reviewed-asr
.venv/Scripts/python.exe tools/check_published_parity.py --exercise drill-01-asr
.venv/Scripts/python.exe -m unittest discover -s tests
```

For a publication made from the reference transcript, omit `--reviewed-asr` to require the exact annotated cue boundaries and text.
