"""Query the same published revision through HTTP and through a local MCP subprocess."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
SERVER_ENV = ("EXERCISE_REVIEW_READER_URL", "EXERCISE_REVIEW_API_EXERCISES", "EXERCISE_REVIEW_ARCHIVE",
              "ROXI_ACCESS_MODE", "ROXI_ACCESS_TOKEN")


def http_json(base: str, path: str, params: dict) -> dict:
    query = f"?{urlencode(params, doseq=True)}" if params else ""
    with urlopen(base + path + query, timeout=10) as response:
        return json.load(response)


def check_original(base: str, evidence: dict) -> None:
    url = base + evidence["original_path"]
    if evidence["source_kind"] == "audio":
        with urlopen(Request(url, headers={"Range": "bytes=0-11"}), timeout=10) as response:
            header = response.read()
            if response.status != 206 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                raise RuntimeError("The published radio link did not yield an original WAV byte range.")
    else:
        with urlopen(url, timeout=10) as response:
            number = evidence["locator"]["line_start"]
            for index, line in enumerate(response, start=1):
                if index == number and line.decode("utf-8-sig").rstrip("\r\n") == evidence["original_text"]:
                    return
                if index >= number:
                    break
        raise RuntimeError("The published chat link did not yield the cited original line.")


async def verify(base: str, exercise_id: str) -> None:
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "review_app.mcp_server"], cwd=ROOT,
        env={key: os.environ[key] for key in SERVER_ENV if key in os.environ},
    )
    prefix = f"/v1/exercises/{exercise_id}/revisions"
    latest = await asyncio.to_thread(http_json, base, prefix + "/latest", {})
    async with Client(server, read_timeout_seconds=10) as client:
        published = await client.list_tools()
        expected = {"get_exercise_revision", "search_records", "list_events", "get_evidence"}
        if not expected <= {tool.name for tool in published.tools} or not all(
            tool.annotations and tool.annotations.read_only_hint and tool.output_schema for tool in published.tools
        ):
            raise RuntimeError("The MCP server did not advertise the four read-only tools and their result schemas.")

        async def equal(tool, path, arguments, params=None):
            http_result, mcp_result = await asyncio.gather(
                asyncio.to_thread(http_json, base, path, params or {}),
                client.call_tool(tool, arguments),
            )
            if mcp_result.is_error or mcp_result.structured_content != http_result:
                raise RuntimeError(f"HTTP and MCP returned different results for {tool}.")
            return http_result

        resolved = await client.call_tool("get_exercise_revision", {"exercise_id": exercise_id})
        if resolved.is_error or resolved.structured_content != latest:
            raise RuntimeError("HTTP and MCP resolved different latest revisions.")
        pinned = {"exercise_id": exercise_id, "revision_id": latest["revision_id"]}
        prefix += f"/{pinned['revision_id']}"
        events = await equal("list_events", prefix + "/events", pinned)

        seen_kinds = set()
        cursor = None
        while True:
            filters = {"limit": 8}
            if cursor:
                filters["cursor"] = cursor
            records = await equal("search_records", prefix + "/records", {**pinned, **filters}, filters)
            for record in records["items"]:
                kind = record["source_kind"]
                if kind in seen_kinds:
                    continue
                evidence_id = record["evidence_id"]
                evidence = await equal("get_evidence", prefix + "/evidence/" + evidence_id,
                                       {**pinned, "evidence_id": evidence_id})
                await asyncio.to_thread(check_original, base, evidence)
                seen_kinds.add(kind)
            cursor = records["next_cursor"]
            if not cursor or seen_kinds == {"audio", "chat"}:
                break

        print(json.dumps({
            "exercise_id": exercise_id, "revision_id": latest["revision_id"], "http_mcp_match": True,
            "accepted_events_on_first_page": len(events["items"]), "original_sources_checked": sorted(seen_kinds),
        }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="HTTP app base URL")
    parser.add_argument("--exercise", default="drill-01", help="Exercise with at least one published revision")
    args = parser.parse_args()
    try:
        asyncio.run(verify(args.url.rstrip("/"), args.exercise))
    except HTTPError as error:
        if error.code == 404:
            parser.exit(1, "No published revision or evidence was found. Publish a draft and check the exercise scope.\n")
        raise


if __name__ == "__main__":
    main()
