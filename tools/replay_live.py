"""Deliver the sandbox NDJSON fixture through the same loopback webhook as live chat."""

import argparse
import json
import os
import sys
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "live-chat"


def webhook_url(base: str, exercise: str, room: str) -> str:
    parsed = urlsplit(base)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("Use a loopback HTTP URL such as http://127.0.0.1:8000.")
    return f"{base.rstrip('/')}/exercises/{exercise}/live/{room}/messages"


def token() -> str:
    value = os.environ.get("ROXI_SANDBOX_TOKEN")
    if not value:
        raise ValueError("Set ROXI_SANDBOX_TOKEN in this shell to match the app.")
    return value


def payload(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()


def deliver(url: str, secret: str, body: bytes) -> tuple[int, dict]:
    request = Request(url, data=body, headers={"Content-Type": "application/json", "X-Roxi-Sandbox-Token": secret})
    with urlopen(request, timeout=10) as response:
        return response.status, json.load(response)


def entries(path: Path) -> list[dict]:
    result = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    clock = [entry["deliver_ms"] for entry in result]
    if not result or any(type(ms) is not int or ms < 0 for ms in clock) or sorted(clock) != clock:
        raise ValueError("Replay deliveries need nonnegative, ascending integer deliver_ms values.")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path, nargs="?", default=FIXTURE / "replay.ndjson")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--exercise", default="west-annex-live")
    parser.add_argument("--room", default="receiving-dock")
    parser.add_argument("--speed", type=float, default=1.0, help="Replay speed multiplier")
    args = parser.parse_args()
    if not 0 < args.speed <= 1000:
        parser.error("--speed must be greater than 0 and at most 1000")
    try:
        url = webhook_url(args.base, args.exercise, args.room)
        secret = token()
        start = monotonic()
        for index, entry in enumerate(entries(args.fixture), 1):
            sleep(max(0.0, start + entry["deliver_ms"] / (1000 * args.speed) - monotonic()))
            status, result = deliver(url, secret, payload(entry["message"]))
            print(json.dumps({"delivery": index, "status": status, **result}), flush=True)
    except (OSError, ValueError, KeyError) as error:
        print(f"Replay failed: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
