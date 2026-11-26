"""Exercise pilot permissions, retention, and real PostgreSQL backup/restore in two disposable databases."""

import asyncio
import base64
import hashlib
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import wave
from datetime import UTC, datetime, timedelta
from http.cookiejar import CookieJar
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from mcp import Client, StdioServerParameters
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from check_published_parity import SERVER_ENV
from seed_history import EXERCISE, FIXTURE, ROOM, ROOT, seed
from smoke_demo import DATABASE_SETTINGS, local_server

OTHER = "west-annex-separate"
WORKSPACE = "receiving"
READER = "source.reader"
REVIEWER = "all.reviewer"
INGESTOR = "room.ingestor"
ADMIN = "pilot.admin"


def signed_in(base, username, password):
    jar = CookieJar()
    browser = build_opener(HTTPCookieProcessor(jar))
    body = urlencode({"username": username, "password": password}).encode()
    request = Request(base + "/login", body, headers={"Origin": base, "Accept": "text/html"})
    with browser.open(request, timeout=10) as response:
        assert response.geturl() == base + "/"
        assert response.headers["Cache-Control"] == "private, no-store"
    assert any(cookie.name == "roxi_session" and cookie.has_nonstandard_attr("HttpOnly") for cookie in jar)
    return browser


def http(base, path, *, token=None, params=None, data=None, headers=None, opener=urlopen, expected=200):
    url = base + path + ("?" + urlencode(params) if params else "")
    headers = dict(headers or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with opener(Request(url, data=data, headers=headers), timeout=10) as response:
            assert response.status == expected, (url, response.status)
            assert response.headers["Cache-Control"] == "private, no-store"
            return response.read()
    except HTTPError as error:
        with error:
            assert error.code == expected, (url, error.code, error.read())
            return error.read()


def result(*args, **kwargs):
    return json.loads(http(*args, **kwargs))


def mcp_server(token=None):
    env = {key: os.environ[key] for key in SERVER_ENV if key in os.environ and key != "ROXI_ACCESS_TOKEN"}
    if token:
        env["ROXI_ACCESS_TOKEN"] = token
    return StdioServerParameters(command=sys.executable, args=["-m", "review_app.mcp_server"], cwd=ROOT, env=env)


def post_message(base, token, message_id, *, status=202):
    payload = {"message_id": message_id, "version": 1, "sent_ms": 240000, "sender": "dispatch",
               "text": "North Shuttle is out of service."}
    return result(base, f"/exercises/{EXERCISE}/live/{ROOM}/messages", token=token, expected=status,
                  headers={"Content-Type": "application/json"}, data=json.dumps(payload).encode())


def capture_backup_radio(folder, base, token):
    from review_app.source_catalog import register

    config = folder / "radio-catalog.json"
    config.write_text(json.dumps([{"source_key": "backup-radio", "label": "Backup radio fixture",
                                   "adapter_key": "completed-radio", "stale_after_seconds": 120}]), encoding="utf-8")
    assert register(EXERCISE, config) == 1
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(bytes(16000))
    raw = audio.getvalue()
    payload = {"schema": "roxi.completed-radio/v1", "clip_id": "backup-clip", "sender": "dock",
               "observed_at": "2026-11-22T15:03:00Z", "wav_base64": base64.b64encode(raw).decode("ascii")}
    prefix = f"/v1/exercises/{EXERCISE}/channels/backup-radio/captures"
    response = result(base, prefix, token=token, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, expected=202)
    assert response["state"] == "pending"
    url = f"{prefix}/{response['capture_id']}/audio"
    assert http(base, url, token=token) == raw
    return {"url": url, "digest": hashlib.sha256(raw).hexdigest()}


async def verify_access(base, pin, reader, reviewer, browser):
    from review_app.pilot_admin import remove_member, revoke_source, revoke_tokens

    prefix = f"/exercises/{EXERCISE}"
    original_query = "/v1/history/query"
    evidence_query = "/v1/history/evidence"
    html = {"Accept": "text/html"}
    assert result(base, original_query, params=pin, expected=401)["error"]["code"] == "unauthenticated"
    home = http(base, "/", opener=browser.open, headers=html)
    assert b"Receiving pilot" in home and b"Separate pilot" not in home
    listed = result(base, prefix + "/sources/catalog.json", token=reader)
    assert {source["source_key"] for source in listed["sources"]} == {"fleet", ROOM}
    http(base, f"/exercises/{OTHER}/sources", token=reader, expected=404)
    http(base, prefix + "/review/extract", token=reader, data=b"{}", headers={"Content-Type": "application/json"},
         expected=404)
    http(base, prefix + "/operations.json", token=reviewer, expected=404)
    http(base, prefix + "/review", token=reviewer)
    http(base, prefix + "/revisions/1", token=reviewer)
    published = f"/v1/exercises/{EXERCISE}/revisions/1"
    record = result(base, published + "/records", token=reviewer)["items"][0]
    published_link = result(base, published + "/evidence/" + record["evidence_id"], token=reviewer)["original_path"]
    http(base, published_link, token=reviewer)
    http(base, published_link, token=reader, expected=404)
    async with Client(mcp_server(reader), read_timeout_seconds=10, cache=None) as client:
        allowed = result(base, original_query, token=reader, params=pin)
        citations = [citation for fact in allowed["facts"] for citation in fact["citations"]]
        assert {c["source"] for c in citations} == {"catalog:fleet", f"chat:{ROOM}"}
        assert all(c["identity"] is None for c in citations)
        assert allowed == (await client.call_tool("query_history", pin)).structured_content
        fleet = next(c for c in citations if c["source"] == "catalog:fleet")
        chat = next(c for c in citations if c["source"].startswith("chat:"))
        params = {**pin, "key": fleet["key"]}
        original = result(base, evidence_query, token=reader, params=params)
        assert original == (await client.call_tool("get_history_evidence", params)).structured_content
        http(base, fleet["original_url"], opener=browser.open)
        http(base, chat["original_url"], opener=browser.open)
        revoke_source(WORKSPACE, READER, EXERCISE, "catalog:fleet")
        http(base, fleet["detail_url"], opener=browser.open, headers=html, expected=404)
        http(base, fleet["original_url"], opener=browser.open, headers={"Range": "bytes=0-15"}, expected=404)
        http(base, evidence_query, token=reader, params=params, expected=404)
        assert (await client.call_tool("get_history_evidence", params)).is_error
        scoped = result(base, original_query, token=reader, params=pin)
        assert scoped == (await client.call_tool("query_history", pin)).structured_content
        assert {c["source"] for fact in scoped["facts"] for c in fact["citations"]} == {f"chat:{ROOM}"}
        sources_html = http(base, prefix + "/sources", opener=browser.open, headers=html)
        assert b"fleet-late.json" not in sources_html and ROOM.encode() in sources_html
        with urlopen(Request(base + prefix + "/live/feed",
                             headers={"Authorization": f"Bearer {reader}", "Last-Event-ID": "0"}), timeout=10) as stream:
            assert stream.readline() == b": resumed\n"
            assert stream.readline() == b"\n"
            revoke_source(WORKSPACE, READER, EXERCISE, f"chat:{ROOM}")
            # The initial cursor may dispatch already-buffered authorized events. A new
            # connection must fail, and the original connection must terminate promptly.
            assert b"pilot-after-revocation" not in stream.read()
        http(base, chat["original_url"], opener=browser.open, expected=404)
        http(base, prefix + "/history/assistant", opener=browser.open, headers=html, expected=404)
        http(base, original_query, token=reader, params=pin, expected=404)
        assert (await client.call_tool("query_history", pin)).is_error
        revoke_tokens(READER)
        http(base, original_query, token=reader, params=pin, expected=401)
        assert (await client.call_tool("query_history", pin)).structured_content["error"]["code"] == "unauthenticated"
    remove_member(WORKSPACE, REVIEWER)
    http(base, published_link, token=reviewer, expected=404)
    http(base, prefix + "/revisions/1", token=reviewer, expected=404)


def restore_check(receipt):
    from review_app import pilot_admin, source_catalog
    from review_app.live_chat import work
    from review_app.storage import connect_owner

    with connect_owner() as conn:
        assert conn.execute("SELECT count(*) AS total FROM pilot_credential").fetchone()["total"] == 0
        checkpoint = conn.execute("SELECT change_seq FROM live_room WHERE exercise_id = %s", (EXERCISE,)).fetchone()
        assert checkpoint["change_seq"] == receipt["chat_checkpoint"]
    admin = pilot_admin.issue_token(ADMIN)
    ingestor = pilot_admin.issue_token(INGESTOR)
    with local_server(pilot=True) as base:
        response = result(base, "/v1/history/evidence", token=admin, params=receipt["evidence"])
        assert response["sha256"] == receipt["digest"]
        assert http(base, response["original_url"], token=admin)
        audio = receipt["queued_radio"]
        assert hashlib.sha256(http(base, audio["url"], token=admin)).hexdigest() == audio["digest"]
        assert result(base, audio["url"].removesuffix("/audio"), token=admin)["state"] == "pending"
        feed = result(base, "/v1/history/changes", token=admin,
                      params={"exercise_id": EXERCISE, "after": receipt["resume"]})
        assert not feed["changes"]
        post_message(base, ingestor, "after-restoration")
        assert work(once=True) >= 1
        resumed = result(base, "/v1/history/changes", token=admin,
                         params={"exercise_id": EXERCISE, "after": receipt["resume"]})
        assert len(resumed["changes"]) >= 1
        assert resumed["changes"][0]["sequence"] == receipt["history_checkpoint"] + 1
        assert resumed["changes"][-1]["event"] == "chat.classified"
        ops = result(base, f"/exercises/{EXERCISE}/operations.json", token=admin)
        assert ops["chat"]["change_seq"] == receipt["chat_checkpoint"] + 1
        stream_url = base + f"/exercises/{EXERCISE}/live/feed"
        request = Request(stream_url, headers={"Authorization": f"Bearer {admin}",
                                               "Last-Event-ID": str(receipt["chat_checkpoint"])})
        with urlopen(request, timeout=5) as stream:
            assert stream.readline() == b": resumed\n"
            assert stream.readline() == b"\n"
            assert stream.readline().decode().strip() == f"id: {receipt['chat_checkpoint'] + 1}"
        assert source_catalog.catalog(EXERCISE)["sources"]


def check(folder: Path, target: str) -> dict:
    from review_app import pilot_admin as admin
    from review_app import pilot_backup, pilot_retention
    from review_app.importer import import_exercise
    from review_app.live_chat import work
    from review_app.pilot_operations import connector_health
    from review_app.review_store import publish, start_or_refresh
    from review_app.source_catalog import record_failure
    from review_app.storage import archive_path, connect_owner, migrate

    with connect_owner() as conn:
        assert not conn.execute("SELECT 1 FROM pg_class AS c JOIN pg_namespace AS n ON c.relnamespace = n.oid "
                                "WHERE n.nspname = 'public' LIMIT 1").fetchone(), "Use a fresh test database."
    migrate()
    receipt = seed()
    draft = start_or_refresh(EXERCISE, None)
    assert publish(EXERCISE, draft["version"]) == 1
    fixture = folder / "other"
    shutil.copytree(FIXTURE / "input", fixture)
    manifest = fixture / "exercise.json"
    definition = json.loads(manifest.read_text(encoding="utf-8"))
    definition.update(exercise_id=OTHER, name="Separate site exercise")
    manifest.write_text(json.dumps(definition), encoding="utf-8")
    import_exercise(manifest)
    admin.workspace(WORKSPACE, "Receiving pilot")
    admin.workspace("separate", "Separate pilot")
    admin.attach(WORKSPACE, EXERCISE)
    admin.attach("separate", OTHER)
    passwords = {username: secrets.token_urlsafe(24) for username in (READER, REVIEWER, INGESTOR, ADMIN)}
    for username, role in ((READER, "reader"), (REVIEWER, "reviewer"), (INGESTOR, "ingest"), (ADMIN, "admin")):
        admin.set_password(username, passwords[username])
        admin.member(WORKSPACE, username, role, all_sources=role in {"reviewer", "admin"})
    for source in ("catalog:fleet", f"chat:{ROOM}"):
        admin.grant_source(WORKSPACE, READER, EXERCISE, source)
    admin.grant_source(WORKSPACE, INGESTOR, EXERCISE, f"chat:{ROOM}")
    reader, reviewer, ingestor, operator = [admin.issue_token(username) for username in passwords]
    pin = {"exercise_id": EXERCISE, "known_at": receipt["known_at"]["after_late"], "as_of": receipt["as_of"]}
    with local_server(pilot=True) as base:
        browser = signed_in(base, READER, passwords[READER])
        http(base, f"/exercises/{EXERCISE}/sources", token=ingestor, expected=404)
        post_message(base, reader, "reader-cannot-ingest", status=404)
        heartbeat = f"/exercises/{EXERCISE}/live/{ROOM}/heartbeat"
        result(base, heartbeat, token=ingestor, data=b"")
        post_message(base, ingestor, "queue-fixture")
        capture = f"/exercises/{EXERCISE}/sources/fleet/captures"
        raw_fleet = (FIXTURE / "fleet-late.json").read_bytes()
        header = {"Content-Type": "application/json"}
        http(base, capture, token=ingestor, headers=header, data=raw_fleet, expected=404)
        admin.grant_source(WORKSPACE, INGESTOR, EXERCISE, "catalog:fleet")
        accepted = result(base, capture, token=ingestor, headers=header, data=raw_fleet, expected=202)
        assert accepted["state"] == "accepted" and accepted["capture_id"] == receipt["late_capture_id"]
        admin.revoke_source(WORKSPACE, INGESTOR, EXERCISE, "catalog:fleet")
        http(base, capture, token=ingestor, headers=header, data=raw_fleet, expected=404)
        record_failure(EXERCISE, "weather", "nws", "Fixture fetch unavailable.")
        ops = result(base, f"/exercises/{EXERCISE}/operations.json", token=operator)
        assert ops["chat"]["connection"] == "connected" and ops["chat"]["pending"] == 1
        assert b"Pending message versions" in http(base, f"/exercises/{EXERCISE}/operations", token=operator)
        assert next(row for row in ops["connectors"] if row["source_key"] == "weather")["status"] == "stale"
        later = connector_health(EXERCISE, datetime.now(UTC) + timedelta(minutes=6))
        assert later["chat"]["connection"] == "disconnected" and later["chat"]["classifier"] == "backlogged"
        assert work(once=True) == 1
        awaitable = verify_access(base, pin, reader, reviewer, browser)
        asyncio.run(awaitable)
        feed = result(base, "/v1/history/changes", token=operator, params={"exercise_id": EXERCISE, "limit": 100})
        assert not feed["has_more"]
        fact = result(base, "/v1/history/query", token=operator, params={**pin, "mode": "conflicts"})["facts"][0]
        first = next(c for c in fact["citations"] if c["source"] == "catalog:fleet")
        backup_pin = {**pin, "key": first["key"]}
        checkpoint = result(base, f"/exercises/{EXERCISE}/operations.json", token=operator)["chat"]["change_seq"]
        queued_radio = capture_backup_radio(folder, base, operator)

    bundle = folder / "backup"
    saved = pilot_backup.backup(bundle, compose=True)
    backup_database = bundle / "database.dump"
    saved_dump = backup_database.read_bytes()
    backup_database.write_bytes(saved_dump + b"changed")
    try:
        pilot_backup.restore(bundle, target, folder / "rejected-archive", compose=True)
        raise AssertionError("The restore accepted a modified database dump.")
    except ValueError as error:
        assert "checksum" in str(error)
    finally:
        backup_database.write_bytes(saved_dump)
    restored_archive = folder / "restored"
    restored = pilot_backup.restore(bundle, target, restored_archive, compose=True)
    assert saved["originals"] == restored["originals"] > 0
    params = {"evidence": backup_pin, "digest": first["sha256"], "chat_checkpoint": checkpoint,
              "queued_radio": queued_radio,
              "history_checkpoint": feed["changes"][-1]["sequence"], "resume": feed["resume_token"]}
    record = folder / "restore-check.json"
    record.write_text(json.dumps(params), encoding="utf-8")
    dbname = conninfo_to_dict(target)["dbname"]
    env = dict(os.environ, EXERCISE_REVIEW_ARCHIVE=str(restored_archive))
    for name in DATABASE_SETTINGS:
        settings = conninfo_to_dict(env[name])
        env[name] = make_conninfo(**(settings | {"dbname": dbname}))
    subprocess.run([sys.executable, __file__, "--restored", str(record)], cwd=ROOT, env=env, check=True)

    pilot_retention.policy(WORKSPACE, 1)
    pilot_retention.close(WORKSPACE, EXERCISE)
    with local_server(pilot=True) as base:
        post_message(base, ingestor, "closed-cannot-write", status=404)
    expires = datetime.now(UTC) + timedelta(days=2)
    with connect_owner() as conn:
        shared = conn.execute("SELECT glossary_sha256 FROM exercise WHERE id = %s", (OTHER,)).fetchone()["glossary_sha256"]
    assert pilot_retention.purge(WORKSPACE)["eligible"] == []
    assert pilot_retention.purge(WORKSPACE, now=expires)["eligible"] == [EXERCISE]
    assert archive_path(first["sha256"]).is_file()
    purged = pilot_retention.purge(WORKSPACE, now=expires, apply=True)
    assert purged["originals_removed"] > 0 and not purged["archive_cleanup_pending"]
    assert not archive_path(first["sha256"]).exists() and archive_path(shared).is_file()
    assert not archive_path(queued_radio["digest"]).exists()
    with local_server(pilot=True) as base:
        http(base, first["original_url"], token=operator, expected=404)
        http(base, queued_radio["url"], token=operator, expected=404)
    return {"workspace_isolation": True, "live_scope_revocation": True, "saved_originals_revoked": True,
            "http_ui_mcp": True, "connector_state": True, "retention": True, "restored_queued_radio": True,
            "restored_originals": saved["originals"], "restored_chat_checkpoint": checkpoint,
            "restored_history_checkpoint": params["history_checkpoint"]}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--restored":
        restore_check(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))
    else:
        target = os.environ.get("ROXI_RESTORE_DATABASE_URL")
        if not target or any(not os.environ.get(name) for name in DATABASE_SETTINGS):
            raise SystemExit("Set the three database URLs and ROXI_RESTORE_DATABASE_URL to fresh disposable databases.")
        os.environ.pop("EXERCISE_REVIEW_API_EXERCISES", None)
        with TemporaryDirectory(prefix="roxi-pilot-check-") as root:
            os.environ["EXERCISE_REVIEW_ARCHIVE"] = str(Path(root) / "archive")
            print(json.dumps(check(Path(root), target), indent=2))
