# Run the live chat sandbox

Roxi accepts local sandbox chat, classifies each version with the exercise glossary and rules, and streams the results into the **Timeline** and **Review queue**. The West Annex room uses fictional warehouse messages.

## Start the app and worker

Install dependencies and start PostgreSQL using the [quickstart](../README.md#quickstart). The live fixture uses chat only, so its setup does not require the audio generator.

From the checkout, migrate the database, import the sandbox exercise, and register its room.

```powershell
.venv/Scripts/python.exe -m review_app migrate
.venv/Scripts/python.exe -m review_app import fixtures/live-chat/input/exercise.json
.venv/Scripts/python.exe -m review_app register-live west-annex-live receiving-dock
```

Start the HTTP app with a sandbox token in its environment. Set the same value in the terminal you use to replay or send messages.

```powershell
$env:ROXI_SANDBOX_TOKEN = '<choose a local sandbox token>'
.venv/Scripts/python.exe -m uvicorn review_app.app:app --host 127.0.0.1 --port 8000
```

In a separate terminal, run the classifier. The worker reads its pending work from PostgreSQL when it starts.

```powershell
.venv/Scripts/python.exe -m review_app classify-live
```

Open the [sandbox timeline](http://127.0.0.1:8000/exercises/west-annex-live) and the [review queue](http://127.0.0.1:8000/exercises/west-annex-live/review). The connection indicator describes the browser's event stream. Messages can be accepted and queued while the classifier is stopped.

## Replay the fixture

Run this in a terminal with `ROXI_SANDBOX_TOKEN` set to the same value as the app.

```powershell
.venv/Scripts/python.exe tools/replay_live.py
```

| Case | Expected result |
| --- | --- |
| Bay 3 report, Bay 2 correction, and Dispatch acknowledgment | Each appears under the corresponding classification and opens the exact original payload. |
| Coffee chat | It appears as **Chatter** on the timeline. |
| Three retransmissions | They return the existing evidence IDs. A whole-fixture retry against the same room adds zero messages. |
| Lift L-7 arrives late | It is marked **Late arrival** and sorted by its original send time. |
| Cart C-17 edit and Returns Cage 1's out-of-order versions | The latest version stays visible. The evidence page links both revisions. |
| Worker stops and restarts | The webhook continues to acknowledge durable receipts. A restarted worker classifies the pending versions and the browser resumes from its last event ID. |

The [NDJSON replay](../fixtures/live-chat/replay.ndjson) has 12 deliveries and the [machine-readable answer key](../fixtures/live-chat/answer-key.json) expects nine classified versions and seven current messages. The replay schedule and scoring files are outside the exercise import manifest.

## Send an original message

```powershell
$headers = @{ 'X-Roxi-Sandbox-Token' = $env:ROXI_SANDBOX_TOKEN }
$address = 'http://127.0.0.1:8000/exercises/west-annex-live/live/receiving-dock/messages'
$message = @{
    message_id = 'manual-bay-note'
    version = 1
    sent_ms = 200000
    sender = 'facilities'
    text = 'Bay 2 is out of service.'
} | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri $address -Headers $headers -ContentType 'application/json' -Body $message
```

Use a stable `message_id` and increment `version` for an edit. The sender and original send time stay fixed. `sent_ms` is elapsed from the start of the exercise. A new version returns HTTP 202 and an evidence ID after PostgreSQL commits it. Repeating the same payload version returns HTTP 200 with that ID. Reusing a version with changed content returns HTTP 409.

## Freeze a snapshot for publication

The live panels show current proposals and their evidence. To use the existing acceptance and publication flow, export a frozen copy with a separate exercise ID.

```powershell
.venv/Scripts/python.exe -m review_app export-live west-annex-live west-annex-live-review .local/west-annex-snapshot
.venv/Scripts/python.exe -m review_app import .local/west-annex-snapshot/exercise.json
```

Open the [review snapshot](http://127.0.0.1:8000/exercises/west-annex-live-review/review), extract proposals, review the links, and publish. The export's `source-map.json` maps each snapshot line to the immutable live payload. Use a new output directory for another capture.

## Verify behavior and measure latency

The GitHub integration workflow runs `tools/check_live_replay.py` on disposable PostgreSQL. The check simulates an abrupt worker exit before commit, starts a new worker, resumes the stream, checks every original payload byte for byte, and publishes a correction from the frozen snapshot.

Run the load check against its own imported and registered sandbox exercise with the app and worker running. The default submits 10 messages per second for 900 seconds and times classification until it reaches the same event feed the browser uses.

```powershell
.venv/Scripts/python.exe tools/measure_live.py --exercise west-annex-load --output .local/live-load-report.json
```

### Milestone 9 acceptance run

On the local Windows workstation with an AMD Ryzen 9 9900X, Docker PostgreSQL 17.12, and one classifier, Roxi acknowledged and streamed **9,000 of 9,000** proposals at **10 messages per second for 15 minutes**. Submission-to-feed p95 was **315.1 ms** and the maximum was **515.2 ms**. A separate 30-second Chrome sample observed 299 rendered proposals at **286 ms p95** from database receipt to rendering. The latency figures use two different measurement points. Both runs used synthetic traffic in a sandbox room.
