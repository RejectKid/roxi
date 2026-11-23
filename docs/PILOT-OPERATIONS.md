# Pilot access and operations

Roxi defaults to authenticated access. An operator provisions accounts and assigns imported exercises to workspaces. Each exercise has one workspace. Existing imports become visible in pilot mode after you attach them.

## Start a workspace

Set up PostgreSQL and Python as in the [quickstart](../README.md#quickstart), migrate, and choose an imported exercise. This example uses the [synthetic history replay](HISTORY-REPLAY.md#run-the-cross-source-replay).

```powershell
$python = '.venv/Scripts/python.exe'
& $python -m review_app migrate
& $python tools/seed_history.py --output .local/history-replay.json
& $python -m review_app pilot workspace receiving 'Receiving pilot'
& $python -m review_app pilot attach receiving west-annex-history
& $python -m review_app pilot account operator
& $python -m review_app pilot member receiving operator admin --all-sources
& $python -m review_app pilot account dock-reader
& $python -m review_app pilot member receiving dock-reader reader
& $python -m review_app pilot grant-source receiving dock-reader west-annex-history chat:receiving-dock
$env:ROXI_ACCESS_MODE = 'pilot'
& $python -m uvicorn review_app.app:app --host 127.0.0.1 --port 8000
```

Open [Roxi sign-in](http://127.0.0.1:8000/login). The workspace screen lists the assigned exercises. Passwords are prompted in the terminal. A password change invalidates existing sessions and API tokens for that account.

| Role | Workspace access |
| --- | --- |
| Reader | Read granted sources and source-backed history. |
| Reviewer | Read granted sources. Review, edit, and publish with whole-exercise access. |
| Ingest | Submit messages and captures to granted live chat rooms and catalog sources using a bearer token. |
| Admin | Read, ingest, and review within the membership scope. Open connector health with whole-exercise access. |

Grant sources as `recorded:<source-key>`, `catalog:<source-key>`, `chat:<room-key>`, or `map:<layer-key>`. `history:registry` grants the whole reviewed alias document for that exercise. Membership with `--all-sources` includes later sources in that workspace. Published revisions, their linked evidence, the original timeline, and the history change feed require whole-exercise access because they combine sources. The source catalog, history assistant, and history HTTP and MCP queries support source-specific access.

Revoke an individual grant with `pilot revoke-source receiving dock-reader west-annex-history chat:receiving-dock`. To narrow a whole-exercise membership, add the sources to retain, then rerun `pilot member` without `--all-sources`. `pilot remove-member` removes workspace access. `pilot revoke-tokens` invalidates every browser session and API token for the account. Saved evidence URLs are checked on each request. Ongoing streams re-check access while polling.

## Connect HTTP, MCP, and ingestion

Run `python -m review_app pilot token <username> --hours 24` with the operator database connection. Provide the resulting token in the HTTP `Authorization: Bearer …` header, as `RoxiClient(token=...)`, or in the stdio MCP process's `ROXI_ACCESS_TOKEN` environment variable. Give agents a separate reader account with the needed source grants. [HTTP and MCP examples](HISTORY-INTERFACES.md)

Use an ingestion account's bearer token for:

| Request | Payload |
| --- | --- |
| `POST /exercises/{id}/live/{room}/messages` | The [chat message JSON](LIVE-DEMO.md). |
| `POST /exercises/{id}/live/{room}/heartbeat` | Empty body. Send a heartbeat at least once a minute for an otherwise quiet connector. |
| `POST /exercises/{id}/sources/{key}/captures` | The registered [adapter’s](SOURCE-CATALOG.md) CSV or JSON payload and corresponding content type. |

Keep PostgreSQL and the app bound to loopback. For an authorized network pilot, terminate HTTPS on a reverse proxy, set `ROXI_TRUSTED_HOSTS` to the exact pilot hostname, preserve the Host header, and accept forwarded headers only from that proxy. Configure distinct database credentials for owner, reader, and editor. Use the owner connection for migrations, backup, retention, and account provisioning. The web service uses the reader and editor connections. Run `python -m review_app classify-live` as a supervised worker using the editor connection. The synthetic quickstart uses explicit `ROXI_ACCESS_MODE=demo` and loopback. Exercises attached to workspaces still require pilot access.

## Monitor connectors

Open **Connector health** as a workspace admin. The corresponding `/exercises/{id}/operations.json` responds with the same state for monitoring. Chat shows heartbeat, last receipt, last classification, pending versions, oldest pending age, and the classifier checkpoint. Roxi marks chat disconnected after five minutes with neither a heartbeat nor a receipt and flags a backlog when the oldest unclassified version exceeds 30 seconds. Catalog feeds use their configured freshness windows, the last successful fetch, and the latest processing gap.

## Apply retention

Retention applies to closed exercises at the workspace's configured age. A freshly closed exercise stays readable until purge. Closing blocks connector and review writes through the app.

```powershell
& $python -m review_app pilot retention receiving --days 30
& $python -m review_app pilot close receiving west-annex-history
& $python -m review_app pilot purge receiving
& $python -m review_app pilot purge receiving --apply
```

The command without `--apply` lists eligible exercises. The apply command deletes the exercise, its derived and published records, and its feed history. It then removes archived originals once no remaining exercise references the same digest. A later apply run retries any filesystem cleanup that failed. Review the eligible exercise list before running apply, and use the same approval and retention policy for backup copies.

## Back up and restore

Create a backup directory on protected storage. With Docker Compose, `--compose` uses the container's PostgreSQL tools. For another deployment, omit it and use compatible `pg_dump` and `pg_restore` binaries on the operator machine. The backup contains a PostgreSQL dump and the archived originals referenced by that snapshot, with SHA-256 verification. It preserves permissions and checkpoints. Sessions and bearer tokens are excluded. [PostgreSQL dump](https://www.postgresql.org/docs/current/app-pgdump.html) · [PostgreSQL restore](https://www.postgresql.org/docs/current/app-pgrestore.html)

```powershell
& $python -m review_app pilot backup .local/receiving-backup --compose
docker compose exec -T db createdb -U review_owner roxi_restore_drill
$env:ROXI_RESTORE_DATABASE_URL = 'postgresql://review_owner:review-local-owner@127.0.0.1:55432/roxi_restore_drill'
& $python -m review_app pilot restore .local/receiving-backup .local/restore-drill-archive --compose
```

Restore trusted Roxi backups into a new database and archive path for each drill. Install the existing owner, reader, and editor roles on a fresh PostgreSQL cluster before restoring. Before switching a pilot to a restored database, reconcile membership and account changes since the snapshot, then issue new tokens. Run the app against the restored reader and editor URLs and set `EXERCISE_REVIEW_ARCHIVE` to the restored archive. Open a saved citation, compare its checksum, resume with the saved history token, and confirm the next new message advances the classifier and history checkpoints.

The automated drill in `tools/check_pilot_operations.py` requires fresh disposable source and restore databases. It exercises the two-workspace revocation test, a queued and disconnected connector, expiration, shared archive retention, and checkpoint resume after restoring the real PostgreSQL dump. [Contributor test setup](../CONTRIBUTING.md#test-a-change)
