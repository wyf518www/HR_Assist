import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from hr_assist import assessment
from hr_assist.assessment import CloudModel
from hr_assist.db import dump
from hr_assist.diagnostics import Diagnostics
from hr_assist.storage import BusinessError


@pytest.fixture
def records(monkeypatch):
    result = []
    monkeypatch.setattr(assessment, "emit", lambda event, **fields: result.append({"event": event, **fields}))
    return result


def install_transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(assessment.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


def model(**overrides):
    settings = {
        "approved": True,
        "endpoint": "https://open.bigmodel.cn/api/paas/v4/chat/completions?private-endpoint=private-value",
        "model": "private-model",
        "timeout_seconds": 300,
        **overrides,
    }
    return CloudModel(settings, "private-api-key")


def events(records):
    return [record["event"] for record in records]


def assert_private_values_absent(records):
    serialized = dump(records)
    for forbidden in ("private", "Authorization", "Bearer", "https://", "open.bigmodel.cn", "候选人姓名", "简历正文", "岗位正文"):
        assert forbidden not in serialized


def test_success_records_timings_sizes_usage_without_changing_request(monkeypatch, records):
    calls = []
    now = [10.0]
    monkeypatch.setattr(assessment, "time", SimpleNamespace(monotonic=lambda: now[0]))
    content = '{"ok":true,"private-result":"候选人姓名"}'
    reasoning = "private-reasoning 简历正文"
    body = {"choices": [{"message": {"content": content, "reasoning_content": reasoning}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20, "private": "private-usage",
                      "completion_tokens_details": {"reasoning_tokens": 3, "private": "private-detail"},
                      "prompt_tokens_details": {"cached_tokens": 5, "private": "private-detail"}}}

    class DelayedBody(httpx.SyncByteStream):
        def __iter__(self):
            now[0] = 12.5
            yield dump(body).encode()
            now[0] = 13.0

    def handler(request):
        calls.append(request)
        now[0] = 12.0
        return httpx.Response(200, headers={"X-Private": "private-header"}, stream=DelayedBody())

    install_transport(monkeypatch, handler)
    result, usage = model().complete("private-system 岗位正文", {"private-payload": "简历正文"})
    assert result == json.loads(content)
    assert usage == body["usage"]
    assert len(calls) == 1
    assert json.loads(calls[0].content) == {
        "model": "private-model", "temperature": 0,
        "messages": [{"role": "system", "content": "private-system 岗位正文"},
                     {"role": "user", "content": dump({"private-payload": "简历正文"})}],
        "response_format": {"type": "json_object"},
    }
    assert events(records) == ["MODEL_REQUEST_STARTED", "MODEL_HEADERS_RECEIVED", "MODEL_RESPONSE_RECEIVED", "MODEL_REQUEST_COMPLETED"]
    first, headers, response, completed = records
    UUID(first["request_id"])
    assert len({record["request_id"] for record in records}) == 1
    assert first["request_bytes"] == len(calls[0].content)
    assert first["system_characters"] == len("private-system 岗位正文")
    assert first["payload_characters"] == len(dump({"private-payload": "简历正文"}))
    assert first["read_timeout_seconds"] == 300 and first["connect_timeout_seconds"] == 30
    assert first["provider"] == "zhipu"
    assert first["model_fingerprint"] == hashlib.sha256(b"private-model").hexdigest()
    assert headers["header_wait_ms"] == 2000 and headers["http_status"] == 200
    assert response["first_byte_ms"] == 2500
    assert response["response_bytes"] == len(dump(body).encode())
    assert completed["elapsed_ms"] == 3000
    assert completed["content_characters"] == len(content)
    assert completed["reasoning_characters"] == len(reasoning)
    assert completed["finish_reason"] == "stop"
    assert {key: completed[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")} == {
        "prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
    assert completed["reasoning_tokens"] == 3 and completed["cached_tokens"] == 5
    assert_private_values_absent(records)


@pytest.mark.parametrize("status,code", [(401, "MODEL_AUTH"), (403, "MODEL_FORBIDDEN"), (404, "MODEL_ENDPOINT"),
    (429, "MODEL_RATE_LIMIT"), (400, "MODEL_REQUEST"), (503, "MODEL_UNAVAILABLE"), (302, "MODEL_HTTP")])
def test_http_rejection_diagnostics_do_not_read_private_error_body(monkeypatch, records, status, code):
    reads = []

    class ErrorBody(httpx.SyncByteStream):
        def __iter__(self):
            reads.append(True)
            yield b"private-upstream-message private-api-key"

    install_transport(monkeypatch, lambda request: httpx.Response(status, stream=ErrorBody()))
    with pytest.raises(BusinessError) as error:
        model().complete("private-system", {"text": "private-resume"})
    assert error.value.code == code
    assert events(records) == ["MODEL_REQUEST_STARTED", "MODEL_HEADERS_RECEIVED", "MODEL_REQUEST_FAILED"]
    assert records[-1]["http_status"] == status
    assert records[-1]["error_code"] == code and records[-1]["exception_type"] == "HTTPStatusError"
    assert records[-1]["response_bytes"] == 0 and not reads
    assert_private_values_absent(records)


@pytest.mark.parametrize("exception,code", [(httpx.ReadTimeout, "MODEL_TIMEOUT"), (httpx.ConnectTimeout, "MODEL_TIMEOUT"),
    (httpx.WriteTimeout, "MODEL_TIMEOUT"), (httpx.PoolTimeout, "MODEL_TIMEOUT"),
    (httpx.ConnectError, "MODEL_CONNECT"), (httpx.RemoteProtocolError, "MODEL_TRANSPORT")])
def test_transport_failure_before_headers_is_correlated_and_not_retried(monkeypatch, records, exception, code):
    calls = []
    now = [0.0]
    monkeypatch.setattr(assessment, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def handler(request):
        calls.append(request)
        now[0] = 313.0
        raise exception("private-api-key private-resume", request=request)

    install_transport(monkeypatch, handler)
    with pytest.raises(BusinessError) as error:
        model().complete("private-system", {})
    assert error.value.code == code and len(calls) == 1
    assert events(records) == ["MODEL_REQUEST_STARTED", "MODEL_REQUEST_FAILED"]
    failed = records[-1]
    assert failed["exception_type"] == exception.__name__ and failed["error_code"] == code
    assert failed["elapsed_ms"] == 313000 and failed["response_bytes"] == 0
    assert "http_status" not in failed and "header_wait_ms" not in failed
    if exception is httpx.ReadTimeout:
        assert "单次读等待上限 300 秒" in error.value.message
        assert "已等待 313 秒" in error.value.message
        assert "客户端已停止等待" in error.value.message
        assert "本次调用已停止" not in error.value.message
    assert_private_values_absent(records)


def test_partial_response_timeout_preserves_byte_count_and_timings(monkeypatch, records):
    now = [0.0]
    monkeypatch.setattr(assessment, "time", SimpleNamespace(monotonic=lambda: now[0]))

    class PartialBody(httpx.SyncByteStream):
        def __iter__(self):
            now[0] = 5.0
            yield b"private-partial-response"
            now[0] = 305.0
            raise httpx.ReadTimeout("private-timeout-message")

    def handler(request):
        now[0] = 2.0
        return httpx.Response(200, stream=PartialBody())

    install_transport(monkeypatch, handler)
    with pytest.raises(BusinessError) as error:
        model().complete("private-system", {})
    assert error.value.code == "MODEL_TIMEOUT"
    assert events(records) == ["MODEL_REQUEST_STARTED", "MODEL_HEADERS_RECEIVED", "MODEL_REQUEST_FAILED"]
    assert records[-1]["response_bytes"] == len(b"private-partial-response")
    assert records[-1]["header_wait_ms"] == 2000
    assert records[-1]["first_byte_ms"] == 5000
    assert records[-1]["elapsed_ms"] == 305000
    assert_private_values_absent(records)


@pytest.mark.parametrize("body,code,finish", [
    ({"choices": [{"message": {"content": None, "reasoning_content": "private-reasoning"}}]}, "MODEL_EMPTY_CONTENT", "missing"),
    ({"choices": [{"message": {"content": '{"ok":'}, "finish_reason": "length"}]}, "MODEL_TRUNCATED", "length"),
    ({"choices": [{"message": {"content": "private-invalid-json"}}]}, "MODEL_INVALID_JSON", "missing"),
    ({"choices": [{"message": {"content": "[]"}}]}, "MODEL_INVALID_JSON", "missing"),
    ({"choices": [{"message": {"content": "{}", "refusal": "private-refusal"}, "finish_reason": "content_filter"}]}, "MODEL_REFUSED", "content_filter"),
    ({"choices": [{"message": {"content": "{}"}, "finish_reason": "sensitive"}]}, "MODEL_REFUSED", "content_filter"),
    ({"choices": []}, "MODEL_RESPONSE_FORMAT", None),
    ({"choices": [{"message": {"content": "{}"}, "finish_reason": {"private": "private"}}]}, "MODEL_RESPONSE_FORMAT", None),
])
def test_invalid_final_answers_report_only_safe_metadata(monkeypatch, records, body, code, finish):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=body))
    with pytest.raises(BusinessError) as error:
        model().complete("private-system", {})
    assert error.value.code == code
    assert events(records) == ["MODEL_REQUEST_STARTED", "MODEL_HEADERS_RECEIVED", "MODEL_RESPONSE_RECEIVED", "MODEL_REQUEST_FAILED"]
    assert records[-1]["error_code"] == code
    if finish is not None:
        assert records[-1]["finish_reason"] == finish
    if code == "MODEL_EMPTY_CONTENT":
        assert records[-1]["content_characters"] == 0
        assert records[-1]["reasoning_characters"] == len("private-reasoning")
    assert_private_values_absent(records)


def test_output_limit_logs_received_size_without_response_content(monkeypatch, records):
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 1_000_001))
    with pytest.raises(BusinessError) as error:
        model().complete("private-system", {})
    assert error.value.code == "MODEL_OUTPUT_LIMIT"
    assert events(records) == ["MODEL_REQUEST_STARTED", "MODEL_HEADERS_RECEIVED", "MODEL_REQUEST_FAILED"]
    assert records[-1]["response_bytes"] == 1_000_001
    assert records[-1]["error_code"] == "MODEL_OUTPUT_LIMIT"
    assert_private_values_absent(records)


def test_arbitrary_upstream_finish_and_usage_values_cannot_enter_diagnostics(monkeypatch, records):
    body = {"choices": [{"message": {"content": "{}", "reasoning_content": {"private": "private"}}, "finish_reason": "private-finish"}],
            "usage": {"prompt_tokens": "private-token-value", "completion_tokens": -1, "total_tokens": True,
                      "completion_tokens_details": {"reasoning_tokens": "private-value"}, "prompt_tokens_details": {"cached_tokens": False}}}
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=body))
    assert model(endpoint="https://open.bigmodel.cn.private.invalid/chat/completions").complete("private-system", {})[0] == {}
    assert records[-1]["finish_reason"] == "unknown"
    assert records[-1]["provider"] == "other" and records[-1]["reasoning_characters"] == 0
    assert not {"prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens", "cached_tokens"}.intersection(records[-1])
    assert_private_values_absent(records)


@pytest.mark.parametrize("settings,code", [({"approved": False}, "MODEL_NOT_CONFIGURED"), ({"endpoint": {"private": "private-invalid"}}, "MODEL_REQUEST")])
def test_preflight_failures_have_start_and_failure_records(monkeypatch, records, settings, code):
    calls = []
    install_transport(monkeypatch, lambda request: calls.append(request))
    with pytest.raises(BusinessError) as error:
        model(**settings).complete("private-system", {})
    assert error.value.code == code
    assert events(records) == ["MODEL_REQUEST_STARTED", "MODEL_REQUEST_FAILED"]
    assert records[-1]["error_code"] == code and not calls
    assert_private_values_absent(records)


def test_diagnostic_failure_does_not_change_model_outcome(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("private-diagnostic-storage")

    monkeypatch.setattr(assessment, "emit", unavailable)
    install_transport(monkeypatch, lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}))
    assert model().complete("private-system", {})[0] == {}
    with pytest.raises(BusinessError) as error:
        model(approved=False).complete("private-system", {})
    assert error.value.code == "MODEL_NOT_CONFIGURED"


def test_model_metadata_survives_real_store_allowlist_and_phase_binding(monkeypatch, tmp_path):
    logger = Diagnostics(tmp_path, lambda: {})
    run_id, application_id = "a" * 32, "b" * 32
    install_transport(monkeypatch, lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": None, "reasoning_content": "private-reasoning"}}]}))
    with pytest.raises(BusinessError):
        with logger.operation("assessment", run_id=run_id, application_id=application_id):
            model().complete("private-system", {"text": "private-resume"})
    days = logger.status()["available_days"]
    assert len(days) == 1
    rows = logger.read(days[0], run_id=run_id)
    assert events(rows) == ["PHASE_STARTED", "MODEL_REQUEST_STARTED", "MODEL_HEADERS_RECEIVED",
                            "MODEL_RESPONSE_RECEIVED", "MODEL_REQUEST_FAILED", "PHASE_FAILED"]
    assert all(row["run_id"] == run_id and row["application_id"] == application_id and row["phase"] == "assessment" for row in rows)
    assert len({row["trace_id"] for row in rows}) == 1
    failed = next(row for row in rows if row["event"] == "MODEL_REQUEST_FAILED")
    UUID(failed["request_id"])
    assert failed["reasoning_characters"] == len("private-reasoning")
    assert failed["content_characters"] == 0 and failed["error_code"] == "MODEL_EMPTY_CONTENT"
    assert failed["finish_reason"] == "missing" and failed["provider"] == "zhipu"
    assert_private_values_absent(rows)
    assert "private" not in logger.export(days[0]).decode()
