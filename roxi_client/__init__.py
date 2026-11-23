"""Typed client for a running Roxi checkout."""

from urllib.parse import urlencode
from urllib.request import urlopen

from pydantic import BaseModel

from review_app.history_models import (
    ChangePage, ChangeRequest, EvidenceRequest, HistoryQuery, HistoryResult, OpenHistoryEvidence,
)


class RoxiClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout: float = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get[T: BaseModel](self, path: str, request: BaseModel, output: type[T]) -> T:
        params = request.model_dump(mode="json", exclude_none=True)
        with urlopen(f"{self.base_url}/v1/history/{path}?{urlencode(params)}", timeout=self.timeout) as response:
            return output.model_validate_json(response.read())

    def query(self, request: HistoryQuery) -> HistoryResult:
        return self._get("query", request, HistoryResult)

    def evidence(self, request: EvidenceRequest) -> OpenHistoryEvidence:
        return self._get("evidence", request, OpenHistoryEvidence)

    def changes(self, request: ChangeRequest) -> ChangePage:
        return self._get("changes", request, ChangePage)
