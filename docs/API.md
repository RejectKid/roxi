# Query Roxi over HTTP and MCP

Start the loopback app and [publish the West Annex demo](DEMO.md#publish-a-reviewed-revision). Resolve the latest revision once, then use the returned revision ID for every query and citation in that session. The HTTP and MCP interfaces support the same published records and scope. [Interactive HTTP API](http://127.0.0.1:8000/docs)

## HTTP example

Run this in PowerShell. It loads the correction visible at exactly 02:29.000, resolves the correction and its linked report, and displays the original-source URLs.

```powershell
$base = 'http://127.0.0.1:8000'
$revisions = "$base/v1/exercises/drill-01/revisions"
$latest = Invoke-RestMethod "$revisions/latest"
$pin = "$revisions/$($latest.revision_id)"
$page = Invoke-RestMethod "$pin/events?subject_id=bay-2&as_of_ms=149000"
$correction = $page.items | Where-Object { $_.event_type -eq 'correction' } | Select-Object -First 1
if (-not $correction) { throw 'Publish the correction from the demo walkthrough first.' }

$ids = $correction.evidence_ids + @($correction.relationships | ForEach-Object { $_.evidence_ids })
foreach ($id in ($ids | Sort-Object -Unique)) {
    $evidence = Invoke-RestMethod "$pin/evidence/$id"
    $path = if ($evidence.playback_path) { $evidence.playback_path } else { $evidence.original_path }
    [pscustomobject]@{
        Excerpt = $evidence.excerpt
        Source = "$base$path"
        Location = $evidence.locator
    }
}
```

Search within that same revision with `GET /v1/exercises/drill-01/revisions/{revision_id}/records?text=Correction&channel=dispatch`. For pages with a `next_cursor`, repeat the query with the same revision and filters and pass that cursor. Use elapsed milliseconds for `as_of_ms`, which includes events at the exact cutoff. Event queries default to accepted decisions.

## Local MCP example

Register the stdio server in a compatible client. Replace `C:\path\to\roxi` with the checkout's absolute path. For custom database or archive locations, supply the same reader settings in both processes.

```json
{
  "mcpServers": {
    "roxi": {
      "command": "C:\\path\\to\\roxi\\.venv\\Scripts\\python.exe",
      "args": ["-m", "review_app.mcp_server"],
      "env": {"PYTHONPATH": "C:\\path\\to\\roxi"}
    }
  }
}
```

| Tool | Example arguments |
| --- | --- |
| `get_exercise_revision` | `{"exercise_id":"drill-01"}` |
| `list_events` | `{"exercise_id":"drill-01","revision_id":1,"subject_id":"bay-2","as_of_ms":149000}` |
| `search_records` | `{"exercise_id":"drill-01","revision_id":1,"text":"Correction","channel":"dispatch"}` |
| `get_evidence` | `{"exercise_id":"drill-01","revision_id":1,"evidence_id":"<ID from a result>"}` |

Use your returned `revision_id` and evidence IDs. MCP evidence returns an HTTP `original_path` relative to the running app. Audio also returns `playback_path` with the clip interval. Chat evidence includes the exact original text and its line number. To check all four tools against their HTTP equivalents, run this from the checkout.

```powershell
.venv/Scripts/python.exe tools/check_published_parity.py --exercise drill-01
```

Both interfaces use the database reader role. Set `EXERCISE_REVIEW_API_EXERCISES` to a comma-separated list in the app and MCP environments to scope these interfaces. The default local configuration allows all imported exercises on the workstation.
