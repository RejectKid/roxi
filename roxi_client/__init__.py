"""Typed client for a running Roxi checkout."""

from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel

from review_app.chat_types import ChatRequest, ChatResponse
from review_app.history_models import (
    ChangePage, ChangeRequest, EvidenceRequest, HistoryAnswer, HistoryAnswerRequest, HistoryQuery, HistoryResult,
    OpenHistoryEvidence,
)
from review_app.query_models import ExerciseRequest, RevisionResult


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RoxiClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout: float = 10, *, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.opener = build_opener(NoRedirect())

    def _read[T: BaseModel](self, path: str, output: type[T], *, data: BaseModel | None = None) -> T:
        headers = self.headers.copy()
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = data.model_dump_json(exclude_none=True).encode("utf-8")
        request = Request(self.base_url + path, data=body, headers=headers)
        with self.opener.open(request, timeout=self.timeout) as response:
            return output.model_validate_json(response.read())

    def _get[T: BaseModel](self, path: str, request: BaseModel, output: type[T]) -> T:
        params = request.model_dump(mode="json", exclude_none=True)
        return self._read(f"/v1/history/{path}?{urlencode(params)}", output)

    def query(self, request: HistoryQuery) -> HistoryResult:
        return self._get("query", request, HistoryResult)

    def evidence(self, request: EvidenceRequest) -> OpenHistoryEvidence:
        return self._get("evidence", request, OpenHistoryEvidence)

    def changes(self, request: ChangeRequest) -> ChangePage:
        return self._get("changes", request, ChangePage)

    def answer(self, request: HistoryAnswerRequest) -> HistoryAnswer:
        return self._read("/v1/history/answer", HistoryAnswer, data=request)

    def latest_revision(self, exercise: ExerciseRequest) -> RevisionResult:
        return self._read(f"/v1/exercises/{exercise.exercise_id}/revisions/latest", RevisionResult)

    def published_answer(self, exercise: ExerciseRequest, request: ChatRequest) -> ChatResponse:
        return self._read(f"/v1/exercises/{exercise.exercise_id}/answers", ChatResponse, data=request)
