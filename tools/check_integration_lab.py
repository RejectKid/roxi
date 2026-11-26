"""Check the reference lab across two real HTTP servers and a disposable database."""

import argparse
import io
import json
import os
import subprocess
import sys
import wave
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from smoke_demo import DATABASE_SETTINGS, ROOT


def send(base, path, body=None, *, headers=None):
    with urlopen(Request(base + path, data=body, headers=headers or {}), timeout=10) as response:
        return response.status, response.read()


def check(folder: Path, browser_script: Path | None):
    os.environ["EXERCISE_REVIEW_ARCHIVE"] = str(folder / "archive")
    os.environ["ROXI_ACCESS_MODE"] = "pilot"
    sys.path.insert(0, str(ROOT))
    from examples.integration_lab.__main__ import loopback_service, provision, speech_worker
    from examples.integration_lab.app import RoxiHTTP, create_app
    from review_app.channel_capture import radio_batch
    from review_app.live_chat import connect_editor

    exercise, reader, writer = provision()
    os.environ["EXERCISE_REVIEW_API_EXERCISES"] = exercise

    with loopback_service() as roxi:
        with loopback_service(create_app(RoxiHTTP(roxi, exercise, reader, writer)), "/api/lab") as lab:
            if browser_script:
                with speech_worker(exercise, "small.en", offline=True):
                    subprocess.run(["node", str(browser_script)], cwd=ROOT, check=True,
                                   env={**os.environ, "ROXI_LAB_URL": lab, "ROXI_LAB_EXERCISE": exercise})
                return {"browser_checked": True}

            def get(path):
                return json.loads(send(lab, path)[1])

            def post(path, data):
                return json.loads(send(lab, path, json.dumps(data).encode(),
                                       headers={"Content-Type": "application/json"})[1])

            assert b"Ask Roxi" in send(lab, "/assistant")[1]
            assert not get("/api/snapshot")["facts"]
            for step in (1, 2, 3):
                assert post(f"/api/replay/{step}", {})["step"] == step
            assert post("/api/replay/3", {})["step"] == 3
            assert get("/api/lab")["step"] == 3
            location = post("/api/answer", {"question": "Where are the vans?"})
            assert "Blue Van" in location["answer"] and "Red Van" in location["answer"]
            assert [fact["records"][0]["position"] for fact in location["result"]["facts"]] == [
                [-84.2 + 6.8e-4, 39.75 + 3.9e-4], [-84.2 + 5.9e-4, 39.75 + 2e-4],
            ]
            conflict = post("/api/answer", {"question": "Which reports disagree?"})
            assert len(conflict["result"]["facts"]) == 1
            assert {item["source"] for item in conflict["result"]["facts"][0]["citations"]} == {
                "catalog:blue-chat", "catalog:red-chat",
            }
            changed = post("/api/answer", {"question": "What changed?"})
            assert {"correction", "acknowledgment"} <= {row["kind"] for fact in changed["result"]["facts"]
                                                        for row in fact["records"]}
            stamp = datetime.now(UTC).isoformat()
            sent = post("/api/chat/red?" + urlencode({"observed_at": stamp}),
                        {"key": "fixture-user-report", "text": "Red Van is in service."})
            assert sent["state"] == "accepted"

            audio = io.BytesIO()
            with wave.open(audio, "wb") as clip:
                clip.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                clip.writeframes(bytes(32000))
            raw = audio.getvalue()
            args = urlencode({"key": "fixture-radio", "observed_at": stamp})
            receipt = json.loads(send(lab, f"/api/microphone/blue?{args}", raw, headers={"Content-Type": "audio/wav"})[1])
            assert receipt["state"] == "pending"
            with connect_editor() as conn:
                assert not radio_batch(conn, lambda _: "", exercise_id="different-lab")
                assert radio_batch(conn, lambda _: "WEBVTT\n\nF1\n00:00:00.000 --> 00:00:00.900\nBlue Van is in service.\n",
                                   exercise_id=exercise)
            assert get(f"/api/microphone/blue/{receipt['capture_id']}")["state"] == "accepted"
            sources = [cite for fact in get("/api/snapshot")["facts"] for cite in fact["citations"]]
            citation = next(item for item in sources if item["source"] == "catalog:blue-radio")
            path = f"/api/evidence/{citation['key']}"
            pin = urlencode({"known_at": citation["known_at"]})
            opened = get(path + "?" + pin)
            assert opened["original_record"]["generated_transcript"] == "Blue Van is in service."
            assert send(lab, path + "/original?" + pin) == (200, raw)
            assert send(lab, path + "/original?" + pin, headers={"Range": "bytes=44-69"}) == (206, raw[44:70])

    return {"tracks": 2, "conflicts": 1, "typed_changes": True, "chat_classified": True,
            "earlier_queue_and_original_wav": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-script", type=Path)
    args = parser.parse_args()
    if not all(os.environ.get(var) for var in DATABASE_SETTINGS):
        raise SystemExit("Set the three Roxi database URLs to a disposable test database.")
    with TemporaryDirectory(prefix="roxi-integration-lab-") as temporary:
        print(json.dumps(check(Path(temporary), args.browser_script), indent=2))
