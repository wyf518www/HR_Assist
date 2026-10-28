from copy import deepcopy
from io import BytesIO
import hashlib
import time

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hr_assist.db import dump
from hr_assist.jd_imports import FIELD_LABELS, validate_extraction
from hr_assist.main import create_app
from hr_assist.schemas import JDImportConfirm, RoleInput
from hr_assist.storage import BusinessError
from conftest import make_pdf
from test_core import configure


TEXT = """岗位：软件工程师
学历：本科及以上
经验：三年以上 Python 开发经验
地点：上海
证书：不限
到岗：一个月内
职责：开发业务服务，维护数据库
技能：Python、SQL，Docker 优先
联系：test@example.invalid 13800000000"""
QUOTES = dict(zip(FIELD_LABELS, TEXT.splitlines()[1:8]))
SPANS = [{"id": "R1", "page": 1, "text": TEXT, "source": "text"}]


def output():
    return {"name": {"value": "软件工程师", "evidence": {"span_id": "R1", "quote": "岗位：软件工程师"}},
            "fields": {k: [{"span_id": "R1", "quote": v}] for k, v in QUOTES.items()}, "multiple_roles": False}


class QuoteModel:
    settings = {"model": "synthetic-jd-model"}

    def __init__(self):
        self.calls = []
        self.fail = False

    def complete(self, system, payload):
        self.calls.append(payload)
        if self.fail:
            raise BusinessError("MODEL_FAILED", "模拟模型中断")
        return output(), {"total_tokens": 42, "upstream_secret": "must-not-be-stored"}


@pytest.fixture
def queue(service, monkeypatch):
    configure(service)
    model = QuoteModel()
    monkeypatch.setattr(service, "model", lambda: model)
    monkeypatch.setattr("hr_assist.jd_imports.extract_file", lambda path: deepcopy(SPANS))
    return service.jd_imports, model


def enqueue_ready(queue):
    imports, model = queue
    row = imports.enqueue(TEXT.encode(), "工程师.txt")
    assert imports.process_next()
    assert imports.get(row["id"])["status"] == "READY"
    return imports.get(row["id"])


def confirmation(row, **kwargs):
    return JDImportConfirm(role=RoleInput(name=row["draft"]["name"], jd_text=row["text"],
                                          fields=row["draft"]["fields"], approved=True), reviewed=True, **kwargs)


def test_extraction_quotes_missing_ocr_and_no_automatic_sales():
    raw = output()
    raw["fields"]["certificates"] = []
    draft = validate_extraction(raw, [{**SPANS[0], "source": "ocr"}])
    assert draft["fields"]["certificates"] == ""
    assert draft["fields"]["skills"].endswith("Docker 优先")
    assert draft["missing"] == ["证书"]
    assert draft["warnings"] and draft["evidence"]["education"][0]["page"] == 1
    assert draft["approved"] is False and draft["is_sales"] is False


@pytest.mark.parametrize("case,code", [("quote", "JD_EVIDENCE_INVALID"), ("span", "JD_EVIDENCE_INVALID"),
    ("name", "JD_NAME_INVALID"), ("multiple", "JD_MULTIPLE_ROLES"), ("free_text", "JD_MODEL_SCHEMA")])
def test_unverifiable_model_outputs_rejected(case, code):
    raw = output()
    if case == "quote": raw["fields"]["skills"][0]["quote"] = "必须精通 Java"
    if case == "span": raw["fields"]["skills"][0]["span_id"] = "R999"
    if case == "name": raw["name"]["value"] = "销售经理"
    if case == "multiple": raw["multiple_roles"] = True
    if case == "free_text": raw["fields"]["education"] = "博士"
    with pytest.raises(BusinessError) as error:
        validate_extraction(raw, SPANS)
    assert error.value.code == code


def test_missing_name_is_not_guessed_from_filename():
    raw = output(); raw["name"] = {"value": "", "evidence": None}
    assert validate_extraction(raw, SPANS)["name"] == ""


def test_original_dedup_redaction_metadata_and_atomic_save(queue, service):
    row = enqueue_ready(queue)
    imports, model = queue
    assert "test@example.invalid" not in dump(model.calls)
    assert "13800000000" not in dump(model.calls)
    assert "must-not-be-stored" not in dump(row)
    assert row["metadata"]["usage"]["total_tokens"] == 42
    duplicate = imports.enqueue(TEXT.encode(), "renamed.txt")
    assert duplicate["duplicate"] and duplicate["id"] == row["id"]
    assert not imports.process_next() and len(model.calls) == 1
    file, path = service.storage.file(row["file_id"])
    assert path.read_bytes() == TEXT.encode()
    assert file["sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
    assert service.roles() == []
    payload = confirmation(row)
    payload.role.source_file_id = "untrusted-other-file"
    result = imports.confirm(row["id"], payload)
    assert imports.get(row["id"])["status"] == "SAVED"
    role = service.roles()[0]
    assert role["source_file_id"] == row["file_id"] and role["approved"]
    assert role["current_version"] == result["version_id"]
    assert imports.confirm(row["id"], payload)["already_saved"]
    assert len(service.db.all("SELECT * FROM jd_versions")) == 1
    assert "text" not in imports.list()[0] and "spans" not in imports.list()[0]


def test_incomplete_remains_draft_and_requires_human_review(queue, service):
    row = enqueue_ready(queue)
    data = confirmation(row)
    data.role.fields.certificates = ""
    with pytest.raises(BusinessError, match="JD 缺失项"):
        queue[0].confirm(row["id"], data)
    assert queue[0].get(row["id"])["status"] == "READY"
    data.role.approved = False
    queue[0].confirm(row["id"], data)
    assert service.roles()[0]["missing"] == ["证书"]
    assert not service.roles()[0]["approved"]
    with pytest.raises(ValidationError):
        JDImportConfirm(role=data.role, reviewed=False)


def test_explicit_update_preserves_history_and_custom_criteria(queue, service, role_data):
    existing = service.save_role(RoleInput(**role_data))
    existing["criteria"][0]["description"] = "已确认的自定义学历细则"
    data = RoleInput(**{**role_data, "criteria": existing["criteria"]})
    existing = service.save_role(data, existing["id"])
    row = enqueue_ready(queue)
    with pytest.raises(BusinessError, match="岗位名称已存在"):
        queue[0].confirm(row["id"], confirmation(row))
    stale = confirmation(row, target_role_id=existing["id"], expected_version="stale-version")
    with pytest.raises(BusinessError, match="当前版本"):
        queue[0].confirm(row["id"], stale)
    with pytest.raises(BusinessError):
        queue[0].confirm(row["id"], confirmation(row, target_role_id=existing["id"]))
    queue[0].confirm(row["id"], confirmation(row, target_role_id=existing["id"], expected_version=existing["current_version"]))
    updated = service.roles()[0]
    assert len(service.db.all("SELECT * FROM jd_versions")) == 3
    assert updated["criteria"][0]["description"] == "已确认的自定义学历细则"
    assert next(c for c in updated["criteria"] if c["id"] == "skills")["description"] == QUOTES["skills"]


def test_long_import_requirements_remain_editable(queue, service):
    row = enqueue_ready(queue)
    data = confirmation(row)
    data.role.fields.skills = "技能要求" * 1000
    queue[0].confirm(row["id"], data)
    saved = service.roles()[0]
    edit = RoleInput(name=saved["name"], fields=saved["fields"], criteria=saved["criteria"], jd_text=saved["jd_text"])
    service.save_role(edit, saved["id"])
    assert len(service.db.all("SELECT * FROM jd_versions")) == 2


def test_per_file_failure_retry_and_manual_fallback(queue, service, monkeypatch):
    imports, model = queue
    bad = imports.enqueue(b"corrupt PDF", "broken.pdf")
    good = imports.enqueue(TEXT.encode(), "engineer.txt")
    def extract(path):
        if path.suffix == ".pdf": raise BusinessError("INVALID_PDF", "损坏文件")
        return deepcopy(SPANS)
    monkeypatch.setattr("hr_assist.jd_imports.extract_file", extract)
    imports.process_next(); imports.process_next()
    assert imports.get(bad["id"])["status"] == "FAILED"
    assert imports.get(good["id"])["status"] == "READY"
    assert len(model.calls) == 1
    with pytest.raises(BusinessError): imports.retry(good["id"])
    model.fail = True
    imports.retry(bad["id"])
    monkeypatch.setattr("hr_assist.jd_imports.extract_file", lambda path: deepcopy(SPANS))
    imports.process_next()
    failed = imports.get(bad["id"])
    assert failed["text"] and failed["status"] == "FAILED"
    data = confirmation(imports.get(good["id"]))
    imports.confirm(bad["id"], data)
    assert imports.get(bad["id"])["status"] == "SAVED"


def test_retry_reuses_text_and_can_succeed(queue, monkeypatch):
    imports, model = queue
    row = imports.enqueue(TEXT.encode(), "jd.txt")
    model.fail = True; imports.process_next()
    model.fail = False
    def unexpected_parse(path): raise AssertionError("must reuse parsed text")
    monkeypatch.setattr("hr_assist.jd_imports.extract_file", unexpected_parse)
    imports.retry(row["id"]); imports.process_next()
    assert imports.get(row["id"])["status"] == "READY"
    assert imports.get(row["id"])["attempts"] == 2
    assert len(model.calls) == 2


def test_unapproved_model_format_size_capacity_and_revocation(service, queue, monkeypatch):
    imports, model = queue
    with pytest.raises(BusinessError): imports.enqueue(b"data", "jd.exe")
    with pytest.raises(BusinessError): imports.enqueue(b"", "jd.txt")
    with pytest.raises(BusinessError): imports.enqueue(b"x" * (service.env.max_upload_bytes + 1), "jd.txt")
    monkeypatch.setattr("hr_assist.jd_imports.MAX_PENDING", 1)
    row = imports.enqueue(TEXT.encode(), "jd.txt")
    with pytest.raises(BusinessError, match="队列"): imports.enqueue(b"another", "jd2.txt")
    config = service.settings(); config["llm"]["approved"] = False; service.db.put("settings", config)
    with pytest.raises(BusinessError, match="批准"): imports.enqueue(b"another", "jd2.txt")
    imports.process_next()
    assert imports.get(row["id"])["status"] == "FAILED" and not model.calls


def test_restart_marks_inflight_interrupted_without_rebilling(queue, service):
    imports, model = queue
    active = imports.enqueue(TEXT.encode(), "active.txt")
    waiting = imports.enqueue((TEXT + "\n备注").encode(), "waiting.txt")
    service.db.execute("UPDATE jd_imports SET status='ANALYZING' WHERE id=?", (active["id"],))
    service.startup()
    deadline = time.monotonic() + 5
    while imports.get(waiting["id"])["status"] != "READY" and time.monotonic() < deadline:
        time.sleep(.02)
    assert imports.get(active["id"])["status"] == "INTERRUPTED"
    assert imports.get(waiting["id"])["status"] == "READY"
    assert len(model.calls) == 1


@pytest.mark.parametrize("extension", ["txt", "docx", "pdf"])
def test_real_extraction_in_background_pipeline(service, monkeypatch, extension):
    configure(service)
    data = TEXT.encode()
    if extension == "docx":
        doc = Document(); doc.add_paragraph(TEXT); buffer = BytesIO(); doc.save(buffer); data = buffer.getvalue()
    if extension == "pdf": data = make_pdf("Python engineer. Requires Python and SQL. Build reliable business services.")
    class Model:
        settings = {"model": "test"}
        def complete(self, system, payload):
            span = payload["jd_spans"][0]
            return {"name": {"value": "", "evidence": None}, "fields": {"skills": [{"span_id": span["id"], "quote": "Python"}]}}, {}
    monkeypatch.setattr(service, "model", Model)
    row = service.jd_imports.enqueue(data, "JD." + extension)
    service.jd_imports.process_next()
    parsed = service.jd_imports.get(row["id"])
    assert parsed["status"] == "READY" and parsed["text"]
    assert parsed["draft"]["fields"]["skills"] == "Python"


def test_import_api_auth_csrf_background_and_confirm(environment, monkeypatch):
    app = create_app(environment)
    service = app.state.service
    configure(service)
    monkeypatch.setattr(service, "model", QuoteModel)
    monkeypatch.setattr("hr_assist.jd_imports.extract_file", lambda path: deepcopy(SPANS))
    with TestClient(app) as client:
        assert client.get('/api/jd/imports').status_code == 401
        assert client.post('/api/jd/imports', files={'file': ('jd.txt', TEXT.encode())}).status_code == 401
        login = client.post('/api/auth/login', json={'username': '管理员', 'password': environment.admin_password})
        assert client.post('/api/jd/imports', files={'file': ('jd.txt', TEXT.encode())}).status_code == 403
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        result = client.post('/api/jd/imports', files={'file': ('jd.txt', TEXT.encode())})
        assert result.status_code == 202
        identity = result.json()['id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = client.get('/api/jd/imports/' + identity).json()
            if row['status'] == 'READY': break
            time.sleep(.02)
        assert row['status'] == 'READY'
        payload = confirmation(row).model_dump()
        assert client.post(f'/api/jd/imports/{identity}/confirm', json={**payload, 'reviewed': False}).status_code == 422
        assert client.post(f'/api/jd/imports/{identity}/confirm', json=payload).status_code == 200
        assert client.post(f'/api/jd/imports/{identity}/retry').status_code == 409
        assert client.get('/api/jd/imports').json()[0]['status'] == 'SAVED'
