# Map and history

Roxi shows civilian facility and fleet observations, weather readings, and classified sandbox chat at two independent cutoffs. Open **Map & history** inside an exercise. The page links every observation to its source and keeps ambiguous identities and conflicting reports visible.

| Filter | What it means |
| --- | --- |
| **Observed through** (`as_of`) | Include events with a source observation time at or before this timestamp. Omit this filter to include untimed chat reports. |
| **Known by** (`known_at`) | Include observations Roxi had captured and processed at or before this timestamp. Use only registry and map revisions published by this cutoff. |

Both cutoffs are inclusive and take ISO timestamps with an explicit UTC offset. The view uses the latest known parser per capture and chat edit per message. Fixed historical cutoffs let a delayed parse, correction, or alias update be inspected without changing the earlier view. **Open pinned view** turns the current result into a link with an explicit knowledge cutoff.

## Run the cross-source replay

Start PostgreSQL and install Roxi from the [quickstart](../README.md#quickstart). The replay uses text files and requires no audio generation. In PowerShell from the checkout, run:

```powershell
.venv/Scripts/python.exe -m review_app migrate
.venv/Scripts/python.exe tools/seed_history.py --output .local/history-replay.json
```

Start the app and open these URLs using the saved cutoffs:

```powershell
$replay = Get-Content -Raw .local/history-replay.json | ConvertFrom-Json
"http://127.0.0.1:8000$($replay.views.before_late)"
"http://127.0.0.1:8000$($replay.views.after_late)"
"http://127.0.0.1:8000$($replay.views.after_revisions)"
```

All three views use the same observed cutoff of **10:05 a.m. Eastern on November 22, 2026**. The fixture's observation times are fictional. The seeder sends the data in order and records your database's actual availability times as `known_at` cutoffs. Repeating the seed retains the same evidence and time pins.

- **Before the late delivery**, the view shows six observations. The 10:03 fleet observation has not arrived. A 10:08 reading from that fleet source has already arrived and is outside the observation cutoff.
- **After the late delivery**, the 10:03 fleet observation becomes the seventh entry and is marked **Late arrival**. Reviewed aliases link the asset register's `wa-unit-12`, fleet ID `van-12`, and chat's North Shuttle report to one delivery van. The fleet reported it operating while chat reported it out of service. Both stay visible with evidence links. Yard Runner remains unresolved.
- **After the revisions**, the same original fleet payload is interpreted with parser version 2. The view also shows the second registry and layout versions. Reloading the earlier pinned link still selects version 1.

The [answer key](../fixtures/history/answer-key.json) and `tools/check_history_view.py` test these cases in disposable PostgreSQL. The same filters also work at `/exercises/{exercise_id}/history/view.json`.

## Review aliases and layers

Publish a reviewed registry JSON file with a version and a reviewer label. An alias names a source and external ID, then lists all reviewed candidate entity IDs. Exactly one candidate resolves a match. Zero candidates leave it unresolved. Several candidates are shown without selecting a winner. Catalog namespaces use `catalog:<source-key>`. Sandbox chat uses `chat:<room-key>` and the subject identifier extracted using the exercise glossary.

```powershell
.venv/Scripts/python.exe -m review_app history registry west-annex-live .local/reviewed-registry.json
.venv/Scripts/python.exe -m review_app history layer west-annex-live receiving 1 2026-11-23T14:00:00Z .local/receiving.geojson --label 'Receiving layout'
```

The offline map accepts a local GeoJSON FeatureCollection with labeled points, lines, and polygons in WGS 84 longitude/latitude order. Each layer version has an effective time and a recorded time. A selected marker opens its timeline entry. Records appear on the map only when their source supplies coordinates. Chat and other unlocated observations remain accessible in the timeline. [GeoJSON format](https://www.rfc-editor.org/rfc/rfc7946.html)

The timeline shows up to 250 observations. Newer captures can supersede an observation from the same source and observation time. Opposite status claims at the same observation time are flagged when they resolve to the same reviewed entity. The view displays both claims and makes no automatic finding about which is correct.
