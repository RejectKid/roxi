# Source catalog and replay

The Sources page lists recorded files, the live sandbox chat room, and registered adapters for civilian data. Open a capture to inspect normalized records, validation issues, parser versions, and its original bytes.

## Load the West Annex sources

Start PostgreSQL and install the app using the [quickstart](../README.md#quickstart). These fixtures use chat, CSV, and JSON. Run the following from the checkout.

```powershell
$exercise = 'west-annex-live'
$fixture = 'fixtures/source-catalog'
.venv/Scripts/python.exe -m review_app migrate
.venv/Scripts/python.exe -m review_app import fixtures/live-chat/input/exercise.json
.venv/Scripts/python.exe -m review_app register-live $exercise receiving-dock
.venv/Scripts/python.exe -m review_app sources register $exercise "$fixture/catalog.json"
.venv/Scripts/python.exe -m review_app sources ingest $exercise assets "$fixture/assets.csv"
.venv/Scripts/python.exe -m review_app sources ingest $exercise sites "$fixture/sites.csv"
.venv/Scripts/python.exe -m review_app sources ingest $exercise weather-replay "$fixture/weather.geojson"
$fleet = .venv/Scripts/python.exe -m review_app sources ingest $exercise civilian-fleet "$fixture/fleet.json" --parser-version 1 | ConvertFrom-Json
.venv/Scripts/python.exe -m review_app sources replay $exercise civilian-fleet $fleet.capture_id --parser-version 2
```

Start the HTTP app using the [quickstart](../README.md#quickstart), then open the [West Annex source catalog](http://127.0.0.1:8000/exercises/west-annex-live/sources). The fleet capture shows both parser versions and one SHA-256 digest. Version 1 preserves the provider's status and speed in mph. Version 2 adds m/s and normalizes `en_route` to `in_transit`. Both point to the same original JSON.

The fixture's observation clock is November 23, 2026. On a later date these observations age according to the configured freshness windows. The sample assets omit an expected pallet jack, and the recorded weather sample has an unavailable wind measurement.

## Fetch public weather

The demo registers the public Dayton station `KDAY`. Fetch its most recent observations with the command below. Each invocation records one attempt, and the command uses the registered station on the official NWS host. [National Weather Service API](https://www.weather.gov/documentation/services-web-api)

```powershell
.venv/Scripts/python.exe -m review_app sources fetch-weather $exercise weather-nws
```

The CSV and fleet commands ingest files when run. The NWS command fetches on demand. Refresh Sources to recompute freshness and see new attempts.

## Inspect bad data and health

```powershell
.venv/Scripts/python.exe -m review_app sources ingest $exercise civilian-fleet "$fixture/fleet-partial.json"
.venv/Scripts/python.exe -m review_app sources ingest $exercise civilian-fleet "$fixture/fleet-invalid.txt"
```

The first capture contains one accepted vehicle observation and one invalid latitude. The second is malformed JSON. The CLI returns exit code 2 for a quarantined capture or unavailable input. In Sources, use **Inspect latest capture** to see its error and download the exact bytes. **Open usable capture** still opens the most recent usable observations. Expand **Recent captures** to find older captures after new data arrives.

| Status | Meaning |
| --- | --- |
| Fresh | Observations are within the source's freshness window and there are no reported gaps. |
| Partial | Usable observations have missing expected IDs or validation issues. |
| Stale | The newest usable observation is too old, its timestamp is too far ahead of the local clock, or the latest attempt failed or was quarantined. Previously usable data remains visible. |
| Missing | The source has yet to deliver usable observations. |
| Recorded | An uploaded exercise file. Coverage can include malformed lines or an unknown exercise-time anchor. |

Last successful fetch means Roxi received bytes. The observation time and parser outcome show whether those bytes contained usable data. Source latency is first capture time minus the newest observation in that capture. Replay leaves both source timestamps intact. Sandbox chat uses recent receipt time and reports its classifier delay where available.

## Adapter contract version 1

An adapter declares its key, input schema, and supported parser versions. It accepts the original bytes and produces normalized records and issues. Each record carries a source-scoped record ID, a subject ID, a kind, an aware observation time, typed fields, and an original locator. Each issue has a code, human-readable detail, and a locator.

| Adapter | Input | Evidence locator |
| --- | --- | --- |
| `assets-csv` | UTF-8 CSV with `asset_id,name,asset_type,site_id,updated_at` | First physical line of the CSV record |
| `sites-csv` | UTF-8 CSV with `site_id,name,latitude,longitude,updated_at` | First physical line of the CSV record |
| `civilian-fleet` | `roxi.civilian-fleet/v1` JSON with a `vehicles` array containing ID, observation time, position, status, and optional speed | JSON pointer such as `/vehicles/0` |
| `nws-observations` | NWS observation GeoJSON FeatureCollection with station, observation time, temperature, and wind measurements | JSON pointer such as `/features/0/properties/windSpeed` |

Use timestamps with a UTC offset. Missing weather readings remain null. Captures support up to 2 MiB. A fetch or file access failure records an attempt with its reason, and captured invalid bytes are quarantined by digest. Reingesting the same bytes reuses the capture and parser result, and records the new fetch attempt. Replay creates a separate derivation for the selected parser and adds no fetch attempt.

The local [catalog JSON](http://127.0.0.1:8000/exercises/west-annex-live/sources/catalog.json) exposes these health summaries. The capture pages show per-version records and exact source download links. The established published-revision HTTP and MCP tools continue to return the reviewed exercise records.
