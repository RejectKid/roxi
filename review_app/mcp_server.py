import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import psycopg
from anyio import to_thread
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ValidationError

from . import query_models as model
from . import query_service as service

EVENT_SCHEMA_URI = "exercise-review://schemas/v1/event"
GLOSSARY_TEMPLATE = "exercise-review://exercises/{exercise_id}/revisions/{revision_id}/glossary"


@dataclass(frozen=True)
class ToolSpec:
    description: str
    arguments: type[BaseModel]
    output: type[BaseModel]
    handler: Callable


TOOLS = {
    "get_exercise_revision": ToolSpec(
        "Resolve the latest published revision and its source coverage for a local exercise.",
        model.ExerciseRequest, model.RevisionResult, service.get_exercise_revision,
    ),
    "search_records": ToolSpec(
        "Search radio transcripts and chat in one published revision. Omit text for its source timeline.",
        model.RecordRequest, model.RecordPage, service.search_records,
    ),
    "list_events": ToolSpec(
        "List events and their evidence links in one published revision. Defaults to accepted events. "
        "as_of_ms is an inclusive exercise-time cutoff.",
        model.EventRequest, model.EventPage, service.list_events,
    ),
    "get_evidence": ToolSpec(
        "Open an evidence reference from a published revision. Returns the exact locator, source excerpt, "
        "and relative HTTP path for the original archived file.",
        model.EvidenceRequest, model.EvidenceResult, service.get_evidence,
    ),
}


async def list_tools(_ctx, _params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[
        types.Tool(name=name, description=spec.description, input_schema=spec.arguments.model_json_schema(),
                   output_schema=spec.output.model_json_schema(),
                   annotations=types.ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
        for name, spec in TOOLS.items()
    ])


def tool_result(result: BaseModel, *, failed: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=result.model_dump_json())],
        structured_content=result.model_dump(mode="json"),
        is_error=failed,
    )


async def call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    spec = TOOLS.get(params.name)
    if spec is None:
        return tool_result(service.invalid("Unknown published-revision tool.").result(), failed=True)
    try:
        arguments = spec.arguments.model_validate(params.arguments or {})
    except ValidationError:
        return tool_result(service.invalid().result(), failed=True)
    try:
        result = await to_thread.run_sync(spec.handler, arguments)
    except service.QueryError as error:
        return tool_result(error.result(), failed=True)
    except psycopg.OperationalError:
        return tool_result(service.unavailable().result(), failed=True)
    return tool_result(result)


async def list_resources(_ctx, _params) -> types.ListResourcesResult:
    return types.ListResourcesResult(resources=[
        types.Resource(uri=EVENT_SCHEMA_URI, name="Published event schema", mime_type="application/schema+json")
    ])


async def list_templates(_ctx, _params) -> types.ListResourceTemplatesResult:
    return types.ListResourceTemplatesResult(resource_templates=[
        types.ResourceTemplate(uri_template=GLOSSARY_TEMPLATE, name="Published exercise glossary",
                               mime_type="application/json")
    ])


async def read_resource(_ctx, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
    uri = str(params.uri)
    if uri == EVENT_SCHEMA_URI:
        return types.ReadResourceResult(contents=[
            types.TextResourceContents(uri=params.uri, mime_type="application/schema+json",
                                       text=json.dumps(model.EventPage.model_json_schema()))
        ])
    parts = urlsplit(uri)
    matched = re.fullmatch(r"/([^/]+)/revisions/([0-9]+)/glossary", parts.path)
    if parts.scheme != "exercise-review" or parts.netloc != "exercises" or not matched or parts.query or parts.fragment:
        raise MCPError(types.ErrorData(code=-32002, message="Resource not found."))
    try:
        request = model.RevisionRequest(exercise_id=matched[1], revision_id=matched[2])
        glossary = await to_thread.run_sync(service.get_glossary, request)
    except ValidationError as error:
        raise MCPError(types.ErrorData(code=-32602, message="Invalid glossary resource URI.")) from error
    except service.QueryError as error:
        raise MCPError(types.ErrorData(code=-32002, message=error.message)) from error
    except psycopg.OperationalError as error:
        raise MCPError(types.ErrorData(code=-32603, message=service.unavailable().message)) from error
    return types.ReadResourceResult(contents=[
        types.TextResourceContents(uri=params.uri, mime_type="application/json", text=json.dumps(glossary))
    ])


async def serve() -> None:
    server = Server(
        "Exercise review",
        version="1.0.0",
        instructions="Read published revisions of completed exercises. Resolve the latest revision once, then pin its ID.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_list_resource_templates=list_templates,
        on_read_resource=read_resource,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(serve())
