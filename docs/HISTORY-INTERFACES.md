# Ask about exercise history

Open **Map & history → Ask about this view** for the selected exercise. Roxi keeps the observed and known cutoffs visible with the question. Try “What was recorded?”, “What changed?”, “Which reports disagree?”, or “Which sources are overdue?” For changes, enter an earlier **Baseline known by**. Each finding links to the original record. Roxi shows gaps and reports that disagree without choosing between them.

## Try the West Annex replay

Run the [history replay](HISTORY-REPLAY.md#run-the-cross-source-replay) and start the app. In PowerShell 7.5 or later, keep the receipt's timestamps as strings so the comparison preserves fractional seconds:

```powershell
$replay = Get-Content -Raw .local/history-replay.json | ConvertFrom-Json -DateKind String
$base = 'http://127.0.0.1:8000'
Start-Process ($base + $replay.views.after_late.Replace('/history?', '/history/assistant?'))
```

“Which reports disagree?” should show the West Annex delivery van with two original citations. “What changed?” with `known_at.before_late` as the baseline should surface the late fleet observation at 15:03 UTC.

## HTTP and Python

Open [Swagger UI](http://127.0.0.1:8000/docs) or download the [OpenAPI schema](http://127.0.0.1:8000/openapi.json). The three versioned GET operations share models with the read-only MCP server.

| HTTP path | MCP tool |
| --- | --- |
| `/v1/history/query` | `query_history` |
| `/v1/history/evidence` | `get_history_evidence` |
| `/v1/history/changes` | `list_history_changes` |

```powershell
$cutoff = [uri]::EscapeDataString($replay.known_at.after_late)
$observed = [uri]::EscapeDataString($replay.as_of)
$query = "exercise_id=$($replay.exercise_id)&known_at=$cutoff&as_of=$observed"
$answer = Invoke-RestMethod "$base/v1/history/query?$query&mode=conflicts"
$citation = $answer.facts[0].citations[0]
Invoke-RestMethod "$base/v1/history/evidence?$query&key=$($citation.key)"
```

Use the parameters on each citation when opening evidence. A change can cite an original from the earlier knowledge cutoff. The evidence operation returns the exact record, locator, parser version when applicable, and SHA-256. If a reviewed alias matched the record to an entity, the response also gives the verified identity and original registry version. The UI links to the original records for human inspection.

Run this Python example from the checkout with the dependencies installed:

```python
import json
from pathlib import Path

from roxi_client import EvidenceRequest, HistoryQuery, RoxiClient

replay = json.loads(Path(".local/history-replay.json").read_text())
client = RoxiClient()
result = client.query(HistoryQuery(exercise_id=replay["exercise_id"], as_of=replay["as_of"],
                                   known_at=replay["known_at"]["after_late"], mode="conflicts"))
for fact in result.facts:
    print(fact.text)
    for cite in fact.citations:
        evidence = client.evidence(EvidenceRequest(exercise_id=cite.exercise_id, as_of=cite.as_of,
                                                   known_at=cite.known_at, key=cite.key))
        print(evidence.original_url, evidence.locator.value)
for gap in result.gaps:
    print(gap)
```

`known_at` is required. `as_of` optionally excludes observations without a usable event timestamp. `entity_id` restricts the result to a reviewed entity. `cursor` and `next_cursor` page the result using the same cutoffs and filters. The history view currently caps an exercise at the newest 250 matching observations and reports truncation as a gap. A missing original or failed digest check also produces a gap. The answer classifier recognizes the four history question types shown above.

## Subscribe to changes

Start with the paginated feed or listen over server-sent events:

```powershell
$page = Invoke-RestMethod "$base/v1/history/changes?exercise_id=$($replay.exercise_id)"
curl.exe -N -H "Last-Event-ID: $($page.resume_token)" "$base/exercises/$($replay.exercise_id)/history/changes/stream"
```

The feed emits `catalog.derived`, `chat.classified`, `registry.published`, and `layer.published`. Treat the sequence as scoped to a single exercise. Save the event ID after successfully handling an event. Pass that ID as `Last-Event-ID` to reconnect or as `after` to page the HTTP or MCP feed. Consumers should be able to process a duplicate if they reconnect from an earlier checkpoint. Use the query and evidence operations for record details. The feed contains committed change references and version metadata.

## Install the agent skill

Copy the [roxi-history skill directory](../skills/roxi-history) into your exercise client's project skill path. Codex reads `.agents/skills/roxi-history`. OpenCode reads `.opencode/skills/roxi-history` and also supports the `.agents/skills/` layout. Keep `references/` beside `SKILL.md`. [Codex skill docs](https://learn.chatgpt.com/docs/build-skills) · [OpenCode skill docs](https://opencode.ai/docs/skills/)

Configure a local stdio MCP server named `roxi` with the checkout's Python executable and the arguments `-m review_app.mcp_server`. Set its working directory to the Roxi checkout or set `PYTHONPATH` to that checkout. Set `EXERCISE_REVIEW_API_EXERCISES=west-annex-history` for this demo. See the [MCP setup](API.md) and [skill test prompt](../skills/roxi-history/references/demo.md). Verify that the client calls both `query_history` and `get_history_evidence` and cites both original records. Use a local model or a provider authorized to receive the exercise evidence.

For Codex, run this from the Roxi checkout:

```powershell
$python = (Resolve-Path .venv/Scripts/python.exe).Path
codex mcp add roxi --env "PYTHONPATH=$((Get-Location).Path)" --env "EXERCISE_REVIEW_API_EXERCISES=west-annex-history" -- $python -m review_app.mcp_server
```

For OpenCode, add this entry to the project's `opencode.json` and replace the example paths with the Roxi checkout and its Python executable. Confirm the connection with `opencode mcp list`. [OpenCode MCP configuration](https://opencode.ai/docs/mcp-servers/)

```json
{
  "mcp": {
    "roxi": {
      "type": "local",
      "command": ["C:/path/to/roxi/.venv/Scripts/python.exe", "-m", "review_app.mcp_server"],
      "environment": {
        "PYTHONPATH": "C:/path/to/roxi",
        "EXERCISE_REVIEW_API_EXERCISES": "west-annex-history"
      }
    }
  }
}
```

Codex and OpenCode both reproduced the West Annex disagreement and cited the original fleet record and chat message. OpenCode with local Qwen 3.5 4B needed follow-up prompts to open both records and print the full citations.

Agensi accepts a ZIP with a `SKILL.md` package and reviews uploads before listing. The skill is suitable for evaluating that route because its instructions and references fit in one directory. Users would still need a running Roxi checkout with their own authorized exercise sources and local MCP connection. Select a distribution license and pricing before submitting. [Agensi seller guide](https://www.agensi.io/sell)

To build that archive locally, run `Compress-Archive -Path skills/roxi-history -DestinationPath .local/roxi-history-skill.zip -Force` from the checkout.

## Try local voice

In a browser on `localhost` or `127.0.0.1`, select **Record question**, allow microphone access, then select **Stop recording**. Roxi transcribes up to 15 seconds of browser-recorded speech and fills the question box for review before you select **Ask**. The app processes that recording in memory using cached Faster Whisper `small.en` weights. To populate the cache, run the [regular transcription setup](PILOT-EVALUATION.md#repeat-the-checks) once without `--offline`.

Select **Read answer aloud** to hear a short briefing with a browser-provided local English voice. The screen keeps the full findings, coverage notes, and citation links visible. Voice playback requires a local English voice exposed by the browser.

Run `python tools/check_history_interfaces.py` using the [disposable database setup](../CONTRIBUTING.md#test-a-change) to verify parity, original evidence, feed resume, and voice request validation.
