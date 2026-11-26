"""Reference host application. Every Roxi read and write here crosses HTTP."""

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as UpstreamRequest, build_opener

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, Field
from starlette.concurrency import run_in_threadpool

from review_app.channel_capture import WAV_LIMIT
from review_app.history_models import ObservationKey
from review_app.query_models import Input, Slug
from roxi_client import NoRedirect

from . import scenario

PUBLIC = Path(__file__).with_name("web")
Crew = Literal["blue", "red"]
Text = Annotated[str, Field(min_length=1, max_length=1000)]
Step = Annotated[int, Field(ge=1, le=len(scenario.STEPS))]
CaptureId = Annotated[int, Field(gt=0, le=2**63 - 1)]


class ChatInput(Input):
    key: Slug
    text: Text


class AskInput(Input):
    question: Text


def now() -> str:
    return datetime.now(UTC).isoformat()


class RoxiHTTP:
    def __init__(self, url, exercise_id, reader, writer):
        self.url = url.rstrip("/")
        self.exercise_id = exercise_id
        self.reader = reader
        self.writer = writer
        self.opener = build_opener(NoRedirect())

    def request(self, path, *, payload=None, write=False, byte_range=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Roxi paths must be relative to the configured origin.")
        headers = {"Authorization": f"Bearer {self.writer if write else self.reader}"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        if byte_range is not None:
            headers["Range"] = byte_range
        try:
            with self.opener.open(UpstreamRequest(self.url + path, data=data, headers=headers), timeout=8) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            with error:
                raise HTTPException(error.code, "Roxi rejected the request. Check source access or capture format.") from None
        except (URLError, TimeoutError):
            raise HTTPException(503, "Roxi could not be reached. Retry the same submission.") from None

    def json(self, path, **kwargs):
        return json.loads(self.request(path, **kwargs)[2])

    def channel(self, source):
        return f"/v1/exercises/{self.exercise_id}/channels/{source}/captures"

    def chat(self, crew, key, text, observed_at):
        return self.json(self.channel(scenario.CHANNELS[crew]), write=True, payload={
            "schema": "roxi.channel-chat/v1", "message_id": key, "version": 1,
            "observed_at": observed_at, "sender": crew, "text": text,
        })

    def fleet(self, vehicles):
        return self.json(f"/exercises/{self.exercise_id}/sources/fleet/captures", write=True,
                         payload={"schema": "roxi.civilian-fleet/v1", "vehicles": vehicles})

    def snapshot(self, **params):
        query = urlencode({"exercise_id": self.exercise_id, "known_at": now(), "limit": 100, **params})
        return self.json(f"/v1/history/query?{query}")

    def evidence(self, key, known_at, as_of):
        args = {"exercise_id": self.exercise_id, "key": key, "known_at": known_at}
        if as_of is not None:
            args["as_of"] = as_of
        return self.json(f"/v1/history/evidence?{urlencode(args)}")


def facts_from_fleet(page):
    latest = {}
    for fact in page["facts"]:
        for record in fact["records"]:
            if record["source"] == "catalog:fleet" and record["position"] and record["entity_id"]:
                latest.setdefault(record["entity_id"], (record, fact))
    return latest


def track_answer(roxi):
    page = roxi.snapshot()
    latest = facts_from_fleet(page)
    sentences, facts = [], []
    for crew, name in scenario.NAMES.items():
        if crew not in latest:
            continue
        row, fact = latest[crew]
        status = row["telemetry_status"].replace("_", " ")
        sentences.append(f"{name} was last observed at the {scenario.label_position(row['position'])}, {status}.")
        facts.append(fact)
    return {"answer": " ".join(sentences) or "Roxi has no fleet positions yet. Advance the replay to send the first update.",
            "result": {**page, "facts": facts, "next_cursor": None}}


def create_app(roxi: RoxiHTTP, *, speech_enabled=True) -> FastAPI:
    app = FastAPI(title="Roxi integration lab", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
    app.mount("/assets", StaticFiles(directory=PUBLIC), name="assets")
    lock = Lock()
    step = 0
    baseline = now()
    replay_times = {}

    @app.middleware("http")
    async def local_request(request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and (origin not in {None, f"{request.url.scheme}://{request.url.netloc}"}
                                         or request.headers.get("sec-fetch-site") == "cross-site"):
            return Response(status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Permissions-Policy": "microphone=(self)",
                                 "Content-Security-Policy": "default-src 'self'; media-src 'self'; object-src 'none'; frame-ancestors 'none'"})
        return response

    @app.get("/")
    @app.get("/assistant")
    def home():
        return FileResponse(PUBLIC / "index.html")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/api/lab")
    def status():
        return {"step": step, "steps": len(scenario.STEPS), "speech_enabled": speech_enabled}

    @app.get("/api/snapshot")
    def snapshot():
        return roxi.snapshot()

    @app.post("/api/replay/{requested_step}")
    def replay(requested_step: Step):
        nonlocal step, baseline
        with lock:
            if requested_step <= step:
                return status()
            if requested_step != step + 1:
                raise HTTPException(409, "Advance the replay one step at a time.")
            started = replay_times.setdefault(requested_step, now())
            blue, red, blue_status, red_status, messages = scenario.STEPS[requested_step - 1]
            result = roxi.fleet([scenario.vehicle("blue", blue, blue_status, started),
                                 scenario.vehicle("red", red, red_status, started)])
            if result["state"] != "accepted":
                raise HTTPException(422, "The fixture fleet update did not pass Roxi validation.")
            for index, (crew, text) in enumerate(messages):
                observed = started if requested_step == 2 else (
                    datetime.fromisoformat(started) + timedelta(milliseconds=index)
                ).isoformat()
                roxi.chat(crew, f"replay-{requested_step}-{index}", text, observed)
            baseline, step = started, requested_step
            return status()

    @app.post("/api/chat/{crew}")
    def chat(crew: Crew, data: ChatInput, observed_at: AwareDatetime):
        return roxi.chat(crew, data.key, data.text, observed_at.isoformat())

    @app.post("/api/microphone/{crew}")
    async def radio(request: Request, crew: Crew, key: Slug, observed_at: AwareDatetime):
        if not speech_enabled:
            raise HTTPException(503, "This lab was started with speech disabled.")
        if request.headers.get("content-type") != "audio/wav":
            raise HTTPException(415, "Upload a PCM WAV.")
        buffer = bytearray()
        async for part in request.stream():
            if len(buffer) + len(part) > WAV_LIMIT:
                raise HTTPException(413, "The WAV exceeds Roxi's capture limit.")
            buffer.extend(part)
        payload = {"schema": "roxi.completed-radio/v1", "clip_id": key, "observed_at": observed_at.isoformat(),
                   "sender": crew, "wav_base64": base64.b64encode(buffer).decode("ascii")}
        return await run_in_threadpool(roxi.json, roxi.channel(scenario.RADIOS[crew]), write=True, payload=payload)

    @app.get("/api/microphone/{crew}/{capture_id}")
    def audio_status(crew: Crew, capture_id: CaptureId):
        return roxi.json(f"{roxi.channel(scenario.RADIOS[crew])}/{capture_id}")

    @app.post("/api/answer")
    def answer(data: AskInput):
        if data.question.strip().lower().rstrip("?") in {"where are the vans", "where are the vans now"}:
            return track_answer(roxi)
        return roxi.json("/v1/history/answer", payload={
            "exercise_id": roxi.exercise_id, "question": data.question, "known_at": now(), "baseline_known_at": baseline,
        })

    @app.get("/api/evidence/{key}")
    def evidence(key: ObservationKey, known_at: AwareDatetime, as_of: AwareDatetime | None = None):
        return roxi.evidence(key, known_at.isoformat(), as_of.isoformat() if as_of else None)

    @app.get("/api/evidence/{key}/original")
    def original(request: Request, key: ObservationKey, known_at: AwareDatetime, as_of: AwareDatetime | None = None):
        record = evidence(key, known_at, as_of)
        code, headers, source = roxi.request(record["original_url"], byte_range=request.headers.get("range"))
        returned = {name: headers[name] for name in ("Content-Type", "Content-Range", "Accept-Ranges")
                    if name in headers}
        return Response(source, status_code=code, headers=returned)

    return app
