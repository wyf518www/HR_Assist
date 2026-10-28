"""JD operation context preserves traceability without persisting document text."""
from copy import deepcopy
import json

import pytest

from hr_assist.diagnostics import Diagnostics, emit
from hr_assist.storage import BusinessError
from test_core import configure
from test_jd_imports import TEXT, SPANS, output


@pytest.fixture
def diagnostic_jd(service, monkeypatch):
    configure(service)
    service.diagnostics = Diagnostics(service.env.state_dir, service.settings)
    monkeypatch.setattr("hr_assist.jd_imports.extract_file", lambda path: deepcopy(SPANS))
    return service.jd_imports


def records(service):
    return [record for day in service.diagnostics.status()["days"]
            for record in service.diagnostics.read(day)]


class LoggedModel:
    settings = {"model": "synthetic-jd-diagnostic"}

    def __init__(self, failure=None):
        self.calls = 0
        self.failure = failure

    def complete(self, system, payload):
        self.calls += 1
        emit("MODEL_REQUEST_STARTED", request_id="a" * 32)
        if self.failure == "timeout":
            raise BusinessError("MODEL_TIMEOUT", "DO_NOT_LOG_secret_and_document")
        if self.failure == "internal":
            raise RuntimeError("DO_NOT_LOG_secret_and_document")
        raw = output()
        if self.failure == "evidence":
            raw["fields"]["skills"][0]["span_id"] = "R-missing"
        emit("MODEL_REQUEST_COMPLETED", request_id="a" * 32)
        return raw, {}


def assert_private(rows):
    serialized = json.dumps(rows, ensure_ascii=False)
    for secret in (TEXT, "test@example.invalid", "13800000000", "DO_NOT_LOG", "隐私岗位.txt"):
        assert secret not in serialized


def test_text_analysis_logs_model_in_jd_phase_without_saving_document(diagnostic_jd, service, monkeypatch):
    model = LoggedModel()
    monkeypatch.setattr(service, "model", lambda: model)
    result = diagnostic_jd.analyze_text(TEXT)
    rows = records(service)
    assert result["draft"]["name"] == "软件工程师"
    assert [r["event"] for r in rows] == ["PHASE_STARTED", "MODEL_REQUEST_STARTED",
                                         "MODEL_REQUEST_COMPLETED", "MODEL_REQUEST_STARTED",
                                         "MODEL_REQUEST_COMPLETED", "PHASE_COMPLETED"]
    assert [r["model_step"] for r in rows if r["event"] == "MODEL_REQUEST_STARTED"] == ["analysis", "review"]
    assert {r["phase"] for r in rows} == {"jd_text"}
    assert len({r["trace_id"] for r in rows}) == 1
    assert rows[0]["input_characters"] == len(TEXT)
    assert rows[-1]["elapsed_ms"] >= 0
    assert model.calls == 2
    assert service.roles() == [] and service.db.all("SELECT * FROM jd_imports") == []
    assert_private(rows)


@pytest.mark.parametrize("failure,code", [("timeout", "MODEL_TIMEOUT"),
                                          ("evidence", "EVIDENCE_SOURCE_INVALID"),
                                          ("internal", "INTERNAL_ERROR")])
def test_text_failures_record_safe_code_and_release_lock(diagnostic_jd, service, monkeypatch, failure, code):
    model = LoggedModel(failure)
    monkeypatch.setattr(service, "model", lambda: model)
    with pytest.raises((BusinessError, RuntimeError)):
        diagnostic_jd.analyze_text(TEXT)
    rows = records(service)
    assert rows[-1]["event"] == "PHASE_FAILED" and rows[-1]["error_code"] == code
    assert rows[-1]["phase"] == "jd_text"
    assert rows[-1]["elapsed_ms"] >= 0
    assert not diagnostic_jd.text_analysis_lock.locked()
    assert model.calls == (2 if failure == "evidence" else 1)
    assert_private(rows)


def test_import_logs_parse_and_model_linked_to_import(diagnostic_jd, service, monkeypatch):
    model = LoggedModel()
    monkeypatch.setattr(service, "model", lambda: model)
    item = diagnostic_jd.enqueue(TEXT.encode(), "隐私岗位.txt")
    assert diagnostic_jd.process_next() is True
    rows = records(service)
    assert {r["jd_import_id"] for r in rows} == {item["id"]}
    assert {r["file_id"] for r in rows} == {item["file_id"]}
    assert {r["phase"] for r in rows} == {"jd_import", "document_parse"}
    outer = [r for r in rows if r["phase"] == "jd_import"]
    assert len({r["trace_id"] for r in outer}) == 1
    assert rows[-1]["event"] == "PHASE_COMPLETED"
    assert diagnostic_jd.get(item["id"])["status"] == "READY"
    assert model.calls == 2
    assert_private(rows)


def test_import_failure_retains_text_and_does_not_retry(diagnostic_jd, service, monkeypatch):
    model = LoggedModel("evidence")
    monkeypatch.setattr(service, "model", lambda: model)
    item = diagnostic_jd.enqueue(TEXT.encode(), "隐私岗位.txt")
    assert diagnostic_jd.process_next() is True
    assert diagnostic_jd.process_next() is False
    rows = records(service)
    assert rows[-1]["phase"] == "jd_import"
    assert rows[-1]["event"] == "PHASE_FAILED"
    assert rows[-1]["error_code"] == "EVIDENCE_SOURCE_INVALID"
    row = diagnostic_jd.get(item["id"])
    assert row["status"] == "FAILED" and row["text"] == TEXT and row["attempts"] == 1
    assert service.storage.file(row["file_id"])[1].read_bytes() == TEXT.encode()
    assert model.calls == 2
    assert_private(rows)


def test_parse_failure_is_distinct_and_prevents_model_call(diagnostic_jd, service, monkeypatch):
    model = LoggedModel()
    monkeypatch.setattr(service, "model", lambda: model)
    def fail_parse(path):
        raise BusinessError("INVALID_PDF", "DO_NOT_LOG_secret_and_document")
    monkeypatch.setattr("hr_assist.jd_imports.extract_file", fail_parse)
    item = diagnostic_jd.enqueue(b"broken", "broken.pdf")
    diagnostic_jd.process_next()
    rows = records(service)
    failed = [r for r in rows if r["event"] == "PHASE_FAILED"]
    assert [r["phase"] for r in failed] == ["document_parse", "jd_import"]
    assert {r["error_code"] for r in failed} == {"INVALID_PDF"}
    assert {r["jd_import_id"] for r in failed} == {item["id"]}
    assert model.calls == 0
    assert_private(rows)
