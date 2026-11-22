# Walk through the West Annex demo

Generate the demo audio, import the exercise, and start the app with the [quickstart](../README.md#quickstart). Open the [West Annex timeline](http://127.0.0.1:8000/exercises/drill-01).

## Review the original sources

1. Find the facilities radio report at **02:04** and select **Play clip**. Facilities reported Bay 3 out of service.
2. Read the chat correction at **02:29** and Dispatch's acknowledgment at **02:40**.
3. Expand **Source coverage** to find the secondary radio recording. You can play it by file offset even though its exercise time is unknown.

## Publish a reviewed revision

Open **Review queue** and select **Extract proposals**. Expand each row below, inspect its **Source evidence**, set the decision to **Accepted**, and select **Save review**. The first three decisions must be saved in order so each link has an accepted target.

| Evidence | Review decision |
| --- | --- |
| Facilities radio F06 at 02:04 | Accept the Bay 3 out-of-service report. |
| Chat line 7 at 02:29 | Accept the replacement subject Bay 2, keep the out-of-service status, and link to the 02:04 report. |
| Chat line 8 at 02:40 | Accept the acknowledgment and link to the 02:29 correction. |
| Chat lines 14 and 15 at 05:40 | Accept both reports. Keep these as separate events. |
| Chat line 16 at 05:48 | Accept the acknowledgment with **Leave unresolved** selected. It has two possible targets. |
| Facilities radio F14 and chat line 20 | Accept both Returns Cage 1 reports. Both claims remain visible as a conflict. |
| Facilities radio F18 | Accept the Cart C-17 staging report. |
| Secondary radio U02 | Accept the Bay 2 in-service report. Its exercise time remains unknown. |

Select **Publish revision**, then open **Assistant**. On a fresh installation your revision is **Revision 1**.

## Ask and open the evidence

| Question | What to look for |
| --- | --- |
| At 02:29.000, which bay did the out-of-service report apply to, and what changed? | The answer says Bay 2 and cites radio F06 and chat line 7. Open both links. |
| Which report does the 05:48 Copy refer to? | The answer leaves the link unresolved and cites both candidates and the acknowledgment. |
| Was Bay 2 confirmed back in service by 12:30? | The answer distinguishes the last aligned report from the untimed secondary clip. |

Look for **Evidence verified** beneath each answer. In the last example the verdict confirms that the evidence supports the statement of uncertainty. It does not confirm when Bay 2 returned to service.

For all eight questions, use the [answer key](../fixtures/drill-01/reference/answer-key.md) or run the [evaluation](PILOT-EVALUATION.md#repeat-the-checks).

## Optional local language model

The walkthrough and API checks use built-in question rules. To try freer wording, start Ollama in local-only mode in one PowerShell window.

```powershell
$env:OLLAMA_NO_CLOUD = '1'
ollama serve
```

In another window, download the model once with `ollama pull qwen3.5:4b`. The assistant connects at `127.0.0.1:11434`. Ask “Who read back the Bay 2 message?” to try model-assisted question routing. [Ollama local mode](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features)
