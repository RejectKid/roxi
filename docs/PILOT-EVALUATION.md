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

## Repeat the checks

Run the following from the repository root. Use a fresh output directory for each transcription run. For the answer-key check, provide the revision number of a reviewed copy of the West Annex drill with the gold reports and acknowledgments accepted and the ambiguity preserved.

```powershell
.venv/Scripts/python.exe -m review_app transcribe fixtures/drill-01/input/exercise.json .local/drill-01-asr-rerun --exercise-id drill-01-asr
.venv/Scripts/python.exe tools/evaluate_drill.py --asr-bundle .local/drill-01-asr-rerun --revision 2
.venv/Scripts/python.exe -m unittest discover -s tests
```
