"""Run the fictional integration lab and its isolated Roxi service."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn

from . import scenario


def provision() -> tuple[str, str, str]:
    from review_app import pilot_admin as admin
    from review_app.history_registry import load_registry
    from review_app.importer import import_exercise
    from review_app.source_catalog import register
    from review_app.storage import migrate

    exercise_id = f"dockside-lab-{secrets.token_hex(5)}"
    with TemporaryDirectory(prefix="roxi-lab-import-") as folder:
        root = Path(folder)

        def write(name, value):
            path = root / name
            path.write_text(json.dumps(value), encoding="utf-8")
            return path

        write("glossary.json", {"subjects": [
            {"subject_id": name, "label": label, "aliases": aliases}
            for name, label, aliases in (
                ("blue", "Blue Van", ["Blue delivery van"]),
                ("red", "Red Van", ["Red delivery van"]),
                ("gate", "Receiving Gate", []),
                ("bay-1", "Loading Bay 1", ["Loading Bay One", "Bay 1", "Bay one"]),
                ("bay-2", "Loading Bay 2", ["Loading Bay Two", "Bay 2", "Bay two"]),
            )
        ]})
        (root / "opening.irc").write_text("[00:00:00] <dock> Starting the Dockside logistics exercise.\n", encoding="utf-8")
        manifest = write("exercise.json", {
            "exercise_id": exercise_id, "name": "Dockside logistics", "glossary_file": "glossary.json",
            "started_at_utc": datetime.now(UTC).isoformat(),
            "sources": [{"source_key": "opening", "kind": "chat", "file": "opening.irc", "time_basis": "exercise_elapsed"}],
        })
        keys = {**dict.fromkeys(scenario.CHANNELS.values(), "channel-chat"),
                **dict.fromkeys(scenario.RADIOS.values(), "completed-radio"), "fleet": "civilian-fleet"}
        catalog = write("catalog.json", [
            {"source_key": key, "label": key.replace("-", " ").capitalize(), "adapter_key": adapter, "stale_after_seconds": 120}
            for key, adapter in keys.items()
        ])
        entities = [{"id": key, "label": label, "kind": "asset"} for key, label in scenario.NAMES.items()]
        entities.extend([{"id": "gate", "label": "Receiving Gate", "kind": "site"},
                         {"id": "bay-1", "label": "Loading Bay 1", "kind": "site"},
                         {"id": "bay-2", "label": "Loading Bay 2", "kind": "site"}])
        aliases = [{"source": "catalog:fleet", "external_id": f"{crew}-vehicle", "entities": [crew]}
                   for crew in scenario.CREWS]
        aliases.extend({"source": f"catalog:{key}", "external_id": entity["id"], "entities": [entity["id"]]}
                       for key, adapter in keys.items() if adapter != "civilian-fleet" for entity in entities)
        registry = write("registry.json", {"version": 1, "reviewer": "Fictional fixture", "entities": entities, "aliases": aliases})

        migrate()
        import_exercise(manifest)
        register(exercise_id, catalog)
        load_registry(exercise_id, registry)
        admin.workspace(exercise_id, "Dockside integration lab")
        admin.attach(exercise_id, exercise_id)
        for role in ("reader", "ingest"):
            account = f"{exercise_id}.{role}"
            admin.set_password(account, secrets.token_urlsafe(24))
            admin.member(exercise_id, account, role, all_sources=role == "reader")
        writer = f"{exercise_id}.ingest"
        for key in keys:
            admin.grant_source(exercise_id, writer, exercise_id, f"catalog:{key}")
        return exercise_id, admin.issue_token(f"{exercise_id}.reader", hours=2), admin.issue_token(writer, hours=2)


@contextmanager
def loopback_service(app="review_app.app:app", health="/healthz"):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 12
            while thread.is_alive() and monotonic() < deadline:
                try:
                    with urlopen(url + health, timeout=1):
                        break
                except (URLError, TimeoutError):
                    sleep(0.1)
            else:
                raise RuntimeError("The local Roxi service failed to start.")
            yield url
        finally:
            server.should_exit = True
            thread.join(timeout=10)


@contextmanager
def speech_worker(exercise_id, model, *, offline=False, enabled=True):
    worker = None
    if enabled:
        command = [sys.executable, "-m", "review_app", "transcribe-live", "--model", model, "--exercise-id", exercise_id]
        if offline:
            command.append("--offline")
        worker = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        yield worker
    finally:
        if worker is not None and worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8181)
    parser.add_argument("--model", default="small.en")
    parser.add_argument("--offline", action="store_true", help="Use cached speech weights only")
    parser.add_argument("--no-speech", action="store_true", help="Run the chat and track demo without a speech model")
    args = parser.parse_args()

    os.environ["ROXI_ACCESS_MODE"] = "pilot"
    os.environ["ROXI_TRUSTED_HOSTS"] = "127.0.0.1,localhost"
    exercise_id, reader, writer = provision()
    os.environ["EXERCISE_REVIEW_API_EXERCISES"] = exercise_id
    from .app import RoxiHTTP, create_app

    with loopback_service() as url, speech_worker(exercise_id, args.model, offline=args.offline, enabled=not args.no_speech):
        print(f"Integration lab: http://127.0.0.1:{args.port}/", flush=True)
        print(f"Assistant-only view: http://127.0.0.1:{args.port}/assistant", flush=True)
        uvicorn.run(create_app(RoxiHTTP(url, exercise_id, reader, writer), speech_enabled=not args.no_speech),
                    host="127.0.0.1", port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
