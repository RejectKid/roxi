"""Measure sandbox webhook delivery to the same live feed Roxi's browser uses."""

import argparse
import json
from math import ceil
from pathlib import Path
from queue import Queue
from threading import Event, Thread
from time import perf_counter, sleep
from urllib.request import urlopen
from uuid import uuid4

from replay_live import deliver, payload, token, webhook_url


def observe(url, prefix, sent, ready, expected, result):
    try:
        delays = {}
        with urlopen(url, timeout=25) as feed:
            for line in feed:
                if not line.startswith(b"data: "):
                    continue
                for item in json.loads(line[6:]):
                    key = item["message_id"]
                    if key.startswith(prefix):
                        if key in delays or item["kind"] != "status_report":
                            raise RuntimeError(f"Unexpected classification or duplicate feed event: {key}")
                        delays[key] = perf_counter() - sent[key]
                ready.set()
                if len(delays) == expected:
                    result.put(delays)
                    return
        raise RuntimeError("The live feed ended before all deliveries were visible.")
    except Exception as error:
        result.put(error)
        ready.set()


def measure(args):
    url = webhook_url(args.base, args.exercise, args.room)
    feed_url = f"{args.base.rstrip('/')}/exercises/{args.exercise}/live/feed"
    prefix = f"measure-{uuid4().hex[:10]}-"
    count = args.rate * args.seconds
    sent = {}
    ready, result = Event(), Queue()
    secret = token()
    Thread(target=observe, args=(feed_url, prefix, sent, ready, count, result), daemon=True).start()
    if not ready.wait(timeout=10):
        raise RuntimeError("The live feed did not send its initial state.")
    start = perf_counter()
    for index in range(count):
        if not result.empty():
            break
        sleep(max(0.0, start + index / args.rate - perf_counter()))
        message_id = f"{prefix}{index + 1:06}"
        message = {"message_id": message_id, "version": 1, "sent_ms": 300_000 + index,
                   "sender": "facilities", "text": "Bay 2 is out of service."}
        sent[message_id] = perf_counter()
        status, _ = deliver(url, secret, payload(message))
        if status != 202:
            raise RuntimeError(f"Expected a new receipt for {message_id}.")
    delivery_seconds = perf_counter() - start
    delays = result.get(timeout=30)
    if isinstance(delays, Exception):
        raise delays

    ordered = sorted(delays.values())
    p95 = ordered[ceil(len(ordered) * 0.95) - 1]
    report = {"exercise": args.exercise, "target_messages_per_second": args.rate, "target_seconds": args.seconds,
              "delivery_seconds": round(delivery_seconds, 3), "messages_acknowledged": count,
              "proposals_observed": len(ordered), "p95_submission_to_feed_ms": round(p95 * 1000, 1),
              "max_submission_to_feed_ms": round(ordered[-1] * 1000, 1)}
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if p95 >= 3 or delivery_seconds > args.seconds + 2:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exercise", required=True)
    parser.add_argument("--room", default="receiving-dock")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--rate", type=int, default=10)
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.rate < 1 or args.seconds < 1:
        parser.error("--rate and --seconds must be positive")
    measure(args)


if __name__ == "__main__":
    main()
