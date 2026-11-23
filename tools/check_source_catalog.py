"""Verify catalog ingestion, evidence, freshness, and versioned replay on disposable PostgreSQL."""

import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import urlopen

from smoke_demo import DATABASE_SETTINGS, ROOT, http_json, local_server

FIXTURES = ROOT / "fixtures" / "source-catalog"
EXERCISE = "source-catalog-ci"
CLOCK = datetime.fromisoformat("2026-11-23T14:30:00+00:00")


def check(folder: Path) -> dict:
    os.environ["EXERCISE_REVIEW_ARCHIVE"] = str(folder / "archive")
    os.environ["EXERCISE_REVIEW_API_EXERCISES"] = EXERCISE
    sys.path.insert(0, str(ROOT))

    from review_app import source_catalog as sources
    from review_app.importer import import_exercise
    from review_app.live_chat import ChatMessage, receive, register_room, work
    from review_app.storage import migrate

    migrate()
    input_folder = folder / "input"
    shutil.copytree(ROOT / "fixtures" / "live-chat" / "input", input_folder)
    manifest = input_folder / "exercise.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.write_text(json.dumps({**payload, "exercise_id": EXERCISE}), encoding="utf-8")
    import_exercise(manifest)
    assert sources.register(EXERCISE, FIXTURES / "catalog.json") == 5
    assert sources.register(EXERCISE, FIXTURES / "catalog.json") == 5

    def state(key, at=CLOCK):
        return next(item for item in sources.catalog(EXERCISE, at)["sources"] if item["source_key"] == key)

    def ingest(key, filename, version=None):
        return sources.ingest_file(EXERCISE, key, FIXTURES / filename, version)

    assert state("assets")["status"] == "missing"
    assets = ingest("assets", "assets.csv")
    sites = ingest("sites", "sites.csv")
    assert (assets.state, assets.records, sites.state, sites.records) == ("accepted", 2, "accepted", 2)
    assert state("assets")["status"] == "partial" and state("assets")["missing_ids"] == ["pallet-jack-07"]
    assert state("sites")["status"] == "fresh"
    asset_rows = sources.capture_details(EXERCISE, "assets", assets.capture_id)["derivations"][0]["records"]
    assert asset_rows[1]["data"]["name"] == "Forklift 04, receiving" and asset_rows[1]["locator"]["value"] == 3
    invalid_csv = folder / "malformed.csv"
    invalid_csv.write_bytes(b'asset_id,name,asset_type,site_id,updated_at\nlift-01,"unclosed')
    csv_receipt = sources.ingest_file(EXERCISE, "assets", invalid_csv)
    assert csv_receipt.state == "quarantined" and state("assets")["record_count"] == 2

    fleet = ingest("civilian-fleet", "fleet.json", 1)
    initial = state("civilian-fleet")
    assert fleet.state == "accepted" and initial["status"] == "fresh" and initial["parser_version"] == 1
    assert state("civilian-fleet", CLOCK + timedelta(hours=2))["status"] == "stale"
    replay = sources.replay(EXERCISE, "civilian-fleet", fleet.capture_id, 2)
    second_replay = sources.replay(EXERCISE, "civilian-fleet", fleet.capture_id, 2)
    replayed = state("civilian-fleet")
    assert replay.capture_id == fleet.capture_id and second_replay.reused_derivation
    assert replayed["parser_version"] == 2
    assert (replayed["last_successful_fetch"], replayed["latency_seconds"]) == (
        initial["last_successful_fetch"], initial["latency_seconds"],
    )
    versions = sources.capture_details(EXERCISE, "civilian-fleet", fleet.capture_id)["derivations"]
    assert [item["parser_version"] for item in versions] == [2, 1]
    assert versions[0]["records"][0]["data"]["speed_m_s"] == 11.176
    assert versions[0]["records"][0]["data"]["status"] == "in_transit"
    assert versions[1]["records"][0]["data"]["status"] == "en_route"
    assert versions[0]["records"][0]["locator"] == {"kind": "json_pointer", "value": "/vehicles/0"}

    partial = ingest("civilian-fleet", "fleet-partial.json")
    assert (partial.state, partial.records, partial.issues) == ("partial", 1, 1)
    duplicate = ingest("civilian-fleet", "fleet.json", 1)
    assert duplicate.capture_id == fleet.capture_id and duplicate.reused_derivation
    assert state("civilian-fleet")["missing_ids"] == ["shuttle-07"]
    assert state("civilian-fleet")["capture_url"].endswith(f"/{partial.capture_id}")
    bad = ingest("civilian-fleet", "fleet-invalid.txt")
    assert bad.state == "quarantined" and state("civilian-fleet")["status"] == "stale"
    assert state("civilian-fleet")["capture_url"].endswith(f"/{partial.capture_id}")
    empty = folder / "empty.json"
    empty.touch()
    assert sources.ingest_file(EXERCISE, "civilian-fleet", empty).state == "quarantined"
    assert any(item["id"] == bad.capture_id and item["state"] == "quarantined"
               for item in state("civilian-fleet")["recent_captures"])

    weather_fixture = (FIXTURES / "weather.geojson").read_bytes()
    weather = ingest("weather-replay", "weather.geojson")
    assert (weather.state, weather.records, weather.issues) == ("partial", 1, 1)
    assert state("weather-replay")["status"] == "partial"
    with patch.object(sources, "request_weather", return_value=weather_fixture) as fetch:
        good = sources.fetch_weather(EXERCISE, "weather-nws")
        fetch.assert_called_once_with("KDAY")
    last_fetch = state("weather-nws")["last_successful_fetch"]
    with patch.object(sources, "request_weather", side_effect=URLError("offline")):
        assert sources.fetch_weather(EXERCISE, "weather-nws").state == "unavailable"
    offline = state("weather-nws")
    assert offline["status"] == "stale" and offline["last_successful_fetch"] == last_fetch
    assert offline["latest_capture_url"].endswith(f"/{good.capture_id}")

    register_room(EXERCISE, "receiving-dock")
    body = b'{"message_id":"catalog-chat","version":1,"sent_ms":0,"sender":"dispatch","text":"Bay 2 is in service."}'
    receive(EXERCISE, "receiving-dock", ChatMessage.model_validate_json(body), body)
    assert work(once=True) == 1
    with local_server() as base:
        prefix = f"/exercises/{EXERCISE}/sources"
        catalog = http_json(base, prefix + "/catalog.json", {})
        assert {item["kind"] for item in catalog["sources"]} == {"adapter", "sandbox-chat", "chat"}
        with urlopen(base + prefix, timeout=5) as page:
            assert b"West Annex fleet" in page.read()
        for key, receipt, filename in (
            ("civilian-fleet", fleet, "fleet.json"), ("civilian-fleet", bad, "fleet-invalid.txt"),
            ("weather-nws", good, "weather.geojson"),
        ):
            address = f"{base}{prefix}/{key}/captures/{receipt.capture_id}"
            with urlopen(address + "/original", timeout=5) as response:
                assert response.read() == (FIXTURES / filename).read_bytes()
        with urlopen(f"{base}{prefix}/civilian-fleet/captures/{fleet.capture_id}", timeout=5) as page:
            rendered = page.read()
            assert b"Parser version 1" in rendered and b"Parser version 2" in rendered

    return {"registered_sources": 5, "csv_records": assets.records + sites.records, "partial_fleet": True,
            "quarantined_payloads_inspectable": True, "weather_unavailable_stale": True,
            "versioned_replay": True, "original_bytes_unchanged": True, "sandbox_in_catalog": True}


if __name__ == "__main__":
    if any(not os.environ.get(name) for name in DATABASE_SETTINGS):
        raise SystemExit("Set the three database URLs to a fresh disposable PostgreSQL database.")
    with TemporaryDirectory(prefix="roxi-catalog-check-") as directory:
        print(json.dumps(check(Path(directory)), indent=2))
