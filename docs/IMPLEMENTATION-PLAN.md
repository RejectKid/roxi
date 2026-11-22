# Roxi Implementation Plan

Build the first release around a 15-minute fictional logistics drill. By the end of the pilot, a reviewer should be able to answer a cross-channel question, listen to the evidence, correct the record, and publish an update. Two independent clients should be able to reproduce the evidence used for the original answer. The [technical specification](PROJECT-SPEC.md) defines the storage, interfaces, and pilot acceptance criteria.

## Milestones

| Milestone | Work | Completion check |
| --- | --- | --- |
| **1. Create the drill and answer key** | Write and record one logistics drill. Produce two WAV recordings with reference WebVTT transcripts, a timestamped chat log, a timing manifest, a glossary, and a separate answer key. Define the manual benchmark using the original sources. | A second reader can reproduce the annotated finding times and evidence clips or lines directly from the files. |
| **2. Establish the data path** | Set up the local application, PostgreSQL migrations, source archive, and import worker. Import WAV, chat, and reference transcript. Persist timing and coverage, then show a synchronized timeline with playback. | Re-importing the same files creates no duplicates. A malformed line is reported. Unaligned audio remains playable at the correct file offset. |
| **3. Add structured review and publication** | Extract the first event types with a glossary and simple rules. Build the proposal queue, the review actions, and immutable publication. Add fixtures for conflict, missing evidence, and cross-channel correction. | A reviewer publishes revision 1, edits a transcript or link, and publishes revision 2. Both versions resolve the proper text and evidence. |
| **4. Ship the developer interfaces** | Expose the four shared-service operations through HTTP and MCP. Publish request and response schemas, an example client, and a minimal local setup guide. Implement revision-aware cursors, consistent errors, and authorization. | Contract tests send equivalent HTTP and MCP requests and compare the returned IDs, timing, coverage, and excerpts. Out-of-scope evidence access is rejected. |
| **5. Add transcription and pilot the agents** | Connect a speech-to-text worker through the same segment format and measure it against the reference transcript. Build a question-answering client and an independent evidence verifier. | Both clients use a pinned reference revision. Every scored factual answer traces to the answer key's expected evidence. Record the transcription errors and verification outcomes. |
| **6. Publish and score a reviewed automatic transcript** | Import the generated WebVTT, review cue boundaries and event links against the original radio and chat, and publish a revision of the automatic-transcript exercise. | The live assistant matches the drill answer key, opens the original clip or exact chat line for every citation, and receives an independent verifier verdict for every case. Record the results and reproduction steps. |
| **7. Publish Roxi on GitHub** | Review the tracked files, history, and fixture provenance. Publish the authored demo fixture and generate the audio locally. Create the public `RejectKid/roxi` repository, use **Roxi** as the public project name, and push `main`. Set the repository description, topics, and the default branch. | A fresh public clone contains the source, documentation, scripted fixture, and audio generator. The generated demo imports successfully. |
| **8. Polish and release the MVP** | Use the published Milestone 6 demo as the MVP baseline. Add a polished README with a screenshot and quickstart, a demo walkthrough, setup and troubleshooting guides, HTTP and MCP examples, contributor instructions, and a changelog. Follow OpenDisNet and BigJsonViewer's separate CI and release workflows. | A new user can run the demo from a fresh clone. Pull-request and push checks cover unit tests, the PostgreSQL import and publication path, and the end-to-end answer and evidence checks. Tag `v0.1.0` publishes a GitHub release with notes and any documented assets. |

Milestones 2 through 4 use the reference transcript so storage, review, and tool behavior can be tested while speech recognition is being evaluated. Keep each milestone runnable locally with the same fixture set.

The MVP ends at the reviewed, cited question-answering demo in Milestone 6. Milestone 7 publishes the project, and Milestone 8 prepares its first polished release. The timeline-versus-structured-view comparison remains part of the broader pilot evaluation.

## First fixture and expected results

Create two short recordings within a 15-minute exercise, plus one IRC-style export. The second recording is an alignment edge case. Use invented names, handles, equipment IDs, and incident details. Put the chat clock and the reference audio start in the manifest. Keep the answer key outside the material that agents index.

| Scenario | Fixture entry | Expected behavior |
| --- | --- | --- |
| Incorrect first report | At `02:04`, the facilities radio says “Bay three is OOS.” | Before the correction, Bay 3 is the subject of an out-of-service report. The source audio is playable. |
| Cross-channel correction | At `02:29`, chat says “Correction, Bay 2 is OOS.” | A reviewed link points from the chat correction to the radio report. The derived view uses Bay 2 after the correction and cites both sources. |
| Explicit acknowledgment | At `02:40`, chat says “Copy, Bay 2 OOS.” | The acknowledgment can link to the preceding correction. It records receipt of the message. |
| Ambiguous reply | Two reports are followed by “Copy” without a subject. | The review queue offers candidate links. The published report states retain their source evidence and the acknowledgment remains unresolved. |
| Independent contradiction | Two sources report different conditions for a bay without an explicit correction. | The timeline shows both with their evidence and flags the conflict. |
| Missing coverage | A separate WAV has a transcript with offsets but lacks a clock anchor. | A reviewer can play exact excerpts. The API reports the coverage gap and excludes these entries from strict time-cutoff claims. |
| Transcript amendment | An initially unclear equipment label is corrected by a reviewer after revision 1. | Revision 2 refers to the edited transcript. Revision 1 still resolves to the earlier transcript and the same original source clip while access is valid. |

Include at least one malformed chat line, one retried import, one conflicting reviewer edit, and two items at the same timestamp. Label only clear ground-truth event and link cases as scoring examples. Record ambiguous cases separately so the evaluation can reward abstaining.

## Implementation work by area

### Storage and import

- Write migrations and the versioned archive interface. Establish exercise-scoped IDs and constraints before creating the API.
- Implement the WAV and chat parsers, reference transcript reader, and capture-time alignment. Compare parser output to known fixture timestamps and offsets.
- Add retries with stable import keys and report recoverable stage failures. Test interrupted transcription, duplicate submissions, and cleanup of abandoned staged files.
- Add the initial query indexes after building the list and search queries. Review query plans on a larger generated fixture before tuning storage.

### Review and publication

- Build the combined timeline with evidence playback first. Add the review queue and glossary-assisted extraction after source navigation works.
- Exercise reviewer conflicts with parallel draft updates and concurrent publication. Test that readers see a complete revision even during publication.
- Add a coverage indicator and visible timing uncertainty. Review keyboard navigation and ensure a transcript remains readable while the matching clip plays.

### HTTP and MCP

- Implement shared query validation, revision resolution, authorization, pagination, and evidence lookup in the service. Generate the OpenAPI contract from the application and map validated request and result types to MCP.
- Provide a small HTTP example and an MCP client configuration once those interfaces run. The example resolves a revision, lists events, and opens supporting evidence using the returned IDs.
- Test pagination across equal and unknown timestamps, incorrect revision IDs, attempts to mix exercise and evidence IDs, and authorization changes after publication. Confirm that an expired or purged source returns a clear unavailable result.

### Agents and evaluation

- Record the source references and tool outcomes for each run. Check citation existence and exercise scope before displaying an answer. Use the independent verifier to check whether the evidence actually supports the claim.
- Measure word error rate alongside critical term accuracy for bay numbers, local abbreviations, and status words. Measure event precision and recall, link precision and recall, and appropriate abstention on the annotated cases.
- Compare the plain synchronized timeline with the structured view. Use equivalent questions or alternate which version reviewers see first so answers learned in one view have less effect on the other. Track time to a correct answer, missed corrections, false links, and answers with unsupported claims.

## Release checks

- [ ] A reviewer can complete import, playback, review, and publication using the local demo instructions.
- [ ] Every citation for the scored questions resolves to the expected original clip or chat lines under valid access.
- [ ] Repeat imports and retried workers produce one visible result per input and processing profile.
- [ ] A published revision reproduces its original evidence after a later trackable correction. Direct source access enforces current permissions on every request.
- [ ] The HTTP example and MCP example return the same IDs and source excerpts for the same revision and filters.
- [ ] The test report records the comparison with the plain timeline and names the errors to address before requesting more data.

## Data for the next evaluation

Use the fictional drill to verify cross-channel links with a known answer key. Use ATCO2's downloadable research subset for transcription stress tests and the IRC Disentanglement corpus for threading tests. If a longer synchronized evaluation is useful, request NIST ASAPS access under its data-use agreement and verify which audio and text scenarios fit the test questions. [ATCO2 data](https://atco2.org/data) · [IRC Disentanglement dataset](https://github.com/jkkummerfeld/irc-disentanglement) · [NIST ASAPS access](https://asapsdata.nist.gov/)

## Decisions to validate during the pilot

| Decision | Starting point | Evidence for changing it |
| --- | --- | --- |
| Transcription engine | Select a locally runnable engine when milestone 5 begins. | Reference transcript errors, critical term accuracy, and hardware throughput. |
| Extraction approach | Start with explicit phrases, aliases, and reviewer-confirmed links. | Missed cases where a model produces reliably better links on held-out examples. |
| Search | Start with keyword search and exact aliases. | Repeated evaluation misses that a tested semantic search method can recover. |
| Deployment | Start on one workstation with synthetic data. | An authorized pilot that requires shared access, a defined retention policy, and a deployment owner. |

## First implementation ticket

**Create `drill-01` and its answer key.** Deliver `exercise.json`, `facilities-radio.wav`, `facilities-radio.vtt`, `unaligned-radio.wav`, `unaligned-radio.vtt`, `dispatch.irc`, `glossary.json`, and an answer key kept outside the import directory. Annotate the original report, correction, and acknowledgment with their expected source locators and write the expected answer before and after `02:29`. The ticket is complete when someone can reproduce both answers directly from the recordings and chat log.
