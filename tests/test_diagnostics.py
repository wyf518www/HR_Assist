from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

import pytest

import hr_assist.diagnostics as module
from hr_assist.diagnostics import Diagnostics, emit
from hr_assist.storage import BusinessError


@pytest.fixture
def log(tmp_path, monkeypatch):
    instant = datetime(2026, 10, 28, 16, 0, tzinfo=module.BEIJING)
    monkeypatch.setattr(Diagnostics, "_at", staticmethod(lambda at=None: instant if at is None else at.astimezone(module.BEIJING)))
    return Diagnostics(tmp_path / "state", lambda: {"diagnostics": {"enabled": True, "retention_days": 7, "cleanup_time": "03:00"}})


def _day(log, day="2026-10-28"):
    return log.read(day, limit=1000)


def _plant(log, day, suffix="", data=b"{}\n"):
    log._directory.mkdir(parents=True, exist_ok=True)
    target = log._directory / f"diagnostic-{day}{suffix}.jsonl"
    target.write_bytes(data)
    return target


def test_write_and_read_allow_only_controlled_metadata(log):
    run_id = uuid.uuid4().hex
    log.write("MODEL_REQUEST_STARTED", run_id=run_id, phase="assessment", provider="zhipu",
              model_fingerprint=hashlib.sha256(b"synthetic-model").hexdigest(), request_bytes=230,
              system_characters=5, read_timeout_seconds=300, api_key="secret-key", endpoint="private-host",
              resume="private-body", candidate_name="private-name", filename="private-file", error_code="PRIVATE_NAME",
              exception_type="PrivateException", http_status="private-status", finish_reason="private-finish")
    record = _day(log)[0]
    assert record["run_id"] == run_id
    assert record["request_bytes"] == 230
    assert record["provider"] == "zhipu"
    assert set(record) == {"timestamp", "event", "run_id", "phase", "provider", "model_fingerprint",
                           "request_bytes", "system_characters", "read_timeout_seconds"}
    assert "private" not in log.export("2026-10-28").decode()
    assert "secret" not in next(log._directory.glob("*.jsonl")).read_text()


@pytest.mark.parametrize("bad", [True, -1, float("nan"), float("inf"), 10**400, "password", {}, []])
def test_numeric_metadata_rejects_invalid_values(log, bad):
    log.write("PHASE_STARTED", elapsed_ms=bad)
    assert "elapsed_ms" not in _day(log)[0]


def test_identifier_and_event_allowlist(log):
    log.write("UNTRUSTED_SECRET_EVENT", run_id=uuid.uuid4().hex)
    log.write("PHASE_STARTED", run_id="../../sensitive", application_id="not-a-uuid",
              model_fingerprint="raw-model-name", phase="sensitive-name", provider="private-provider")
    assert set(_day(log)[0]) == {"event", "timestamp"}


def test_operation_context_is_nested_and_restored(log):
    run_id = uuid.uuid4().hex
    emit("MODEL_REQUEST_STARTED", request_bytes=100)  # No operation, no global logger.
    with log.operation("jd_import", run_id=run_id) as outer:
        emit("MODEL_REQUEST_STARTED", request_id=uuid.uuid4().hex)
        with log.operation("document_parse", file_id=uuid.uuid4().hex) as inner:
            emit("MODEL_RESPONSE_RECEIVED", response_bytes=200)
        emit("MODEL_REQUEST_COMPLETED", elapsed_ms=30)
    emit("MODEL_REQUEST_STARTED")
    records = _day(log)
    assert len(records) == 7
    assert inner != outer
    assert {record["run_id"] for record in records} == {run_id}
    assert records[3]["trace_id"] == inner
    assert records[5]["trace_id"] == outer
    assert records[6]["event"] == "PHASE_COMPLETED"
    assert records[6]["elapsed_ms"] >= 0


def test_context_isolated_between_threads(log):
    barrier = threading.Barrier(2)
    ids = [uuid.uuid4().hex, uuid.uuid4().hex]

    def worker(identity):
        with log.operation("assessment", run_id=identity):
            barrier.wait(timeout=5)
            emit("MODEL_REQUEST_STARTED")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, ids))
    rows = _day(log)
    assert len(rows) == 6
    for identity in ids:
        selected = [row for row in rows if row["run_id"] == identity]
        assert len(selected) == 3
        assert len({row["trace_id"] for row in selected}) == 1


def test_failure_records_code_and_frames_never_message_source_or_locals(log):
    private_local = "candidate contact private-secret@example.test"
    with pytest.raises(BusinessError):
        with log.operation("assessment"):
            raise BusinessError("MODEL_TIMEOUT", private_local)
    record = _day(log)[-1]
    assert record["event"] == "PHASE_FAILED"
    assert record["error_code"] == "MODEL_TIMEOUT"
    assert record["exception_type"] == "BusinessError"
    assert record["stack"]
    assert all(set(frame) == {"file", "function", "line"} for frame in record["stack"])
    output = log.export("2026-10-28").decode()
    assert private_local not in output
    assert str(log._state) not in output
    assert "raise BusinessError" not in output


def test_boundary_failure_handles_unknown_codes_and_exception_classes(log):
    class PrivateCandidateError(Exception):
        code = "CANDIDATE_SECRET"
    log.failure("preflight", PrivateCandidateError("secret-content"), run_id=uuid.uuid4().hex)
    record = _day(log)[0]
    assert record["error_code"] == "INTERNAL_ERROR"
    assert record["exception_type"] == "OtherException"
    assert "secret" not in log.export("2026-10-28").decode().lower()


def test_logging_failure_never_replaces_business_result(log, monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError("private path")
    monkeypatch.setattr(log, "_open", fail)
    with log.operation("identity"):
        result = 42
    assert result == 42
    with pytest.raises(BusinessError, match="original business error"):
        with log.operation("identity"):
            raise BusinessError("MODEL_TIMEOUT", "original business error")
    assert log.status()["write_error"] == "WRITE_FAILED"


def test_rotation_and_daily_cap_are_bounded(log, monkeypatch):
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 320)
    monkeypatch.setattr(module, "MAX_FILES_PER_DAY", 3)
    for index in range(30):
        log.write("PHASE_STARTED", item_count=index)
    files = list(log._directory.glob("*.jsonl"))
    assert len(files) == 3
    assert all(path.stat().st_size <= 320 for path in files)
    assert log.status()["daily_capped"] is True
    assert log.status()["write_error"] == "DAY_CAP_REACHED"


def test_read_is_bounded_and_filters_by_exact_run(log):
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    for index in range(6):
        log.write("PHASE_STARTED", run_id=a if index % 2 == 0 else b, item_count=index)
    assert [row["item_count"] for row in log.read("2026-10-28", limit=2)] == [4, 5]
    assert [row["item_count"] for row in log.read("2026-10-28", limit=2, run_id=a)] == [2, 4]
    exported = [json.loads(line) for line in log.export("2026-10-28", run_id=b).splitlines()]
    assert [row["item_count"] for row in exported] == [1, 3, 5]
    assert log.read("2026-10-27") == []


def test_read_resanitizes_existing_files_and_skips_malformed_lines(log):
    raw = {"timestamp": "2026-10-28T10:00:00+08:00", "event": "PHASE_STARTED", "phase": "identity",
           "resume": "secret-body", "exception_type": "private-name", "stack": [{"file": "C:/secret.py", "function": "f", "line": 1}]}
    _plant(log, "2026-10-28", data=b"not-json\n[]\n" + json.dumps(raw).encode() + b"\n")
    assert len(_day(log)) == 1
    assert set(_day(log)[0]) == {"timestamp", "event", "phase"}
    assert "secret" not in log.export("2026-10-28").decode()


@pytest.mark.parametrize("day,run_id", [("../2026-10-28", None), ("2026-02-30", None), ("2026-10-28", "private-name"), (None, None)])
def test_read_and_export_reject_path_or_filter_injection(log, day, run_id):
    for method in (log.read, log.export):
        with pytest.raises(BusinessError) as error:
            method(day, run_id=run_id)
        assert error.value.code == "DIAGNOSTIC_QUERY"


def test_export_size_limit_and_no_export_file(log, monkeypatch):
    for _ in range(5):
        log.write("PHASE_STARTED", phase="identity")
    before = {path.name for path in log._directory.iterdir()}
    monkeypatch.setattr(module, "MAX_EXPORT_BYTES", 100)
    with pytest.raises(BusinessError) as error:
        log.export("2026-10-28")
    assert error.value.code == "DIAGNOSTIC_EXPORT_LIMIT"
    assert {path.name for path in log._directory.iterdir()} == before


def test_cleanup_beijing_boundary_and_retained_calendar_days(log):
    expired = _plant(log, "2026-10-21")
    rotated = _plant(log, "2026-10-21", ".01")
    oldest_retained = _plant(log, "2026-10-22")
    current = _plant(log, "2026-10-28")
    # UTC Oct 27 18:59 is Beijing Oct 28 02:59.
    assert log.cleanup_due(datetime(2026, 10, 27, 18, 59, tzinfo=timezone.utc))["performed"] is False
    result = log.cleanup_due(datetime(2026, 10, 27, 19, 0, tzinfo=timezone.utc))
    assert result == {"performed": True, "deleted_files": 2, "last_cleanup": "2026-10-28"}
    assert not expired.exists() and not rotated.exists()
    assert oldest_retained.exists() and current.exists()
    assert log.cleanup_due(datetime(2026, 10, 28, 18, 0, tzinfo=module.BEIJING))["performed"] is False


def test_cleanup_uses_filename_not_mtime_and_catches_up_after_restart(log):
    old = _plant(log, "2026-10-01")
    recent = _plant(log, "2026-10-28")
    os.utime(recent, (0, 0))
    result = log.cleanup_due(datetime(2026, 10, 28, 16, 0, tzinfo=module.BEIJING))
    assert result["deleted_files"] == 1
    assert recent.exists() and not old.exists()
    restarted = Diagnostics(log._state, lambda: {})
    assert restarted.status()["last_cleanup"] == "2026-10-28"
    assert restarted.cleanup_due(datetime(2026, 10, 28, 20, 0, tzinfo=module.BEIJING))["performed"] is False
    assert restarted.cleanup_due(datetime(2026, 10, 29, 3, 0, tzinfo=module.BEIJING))["performed"] is True


def test_cleanup_does_not_touch_other_files_or_business_audit(log):
    _plant(log, "2026-10-01")
    other = log._directory / "diagnostic-2026-10-01.jsonl.bak"
    other.write_text("must survive")
    invalid_day = _plant(log, "2026-02-30")
    business = log._state / "logs"
    business.mkdir()
    business_file = business / "diagnostic-2026-10-01.jsonl"
    business_file.write_text("audit must survive")
    db = log._state / "app.sqlite3"
    db.write_text("database must survive")
    assert log.cleanup_due()["deleted_files"] == 1
    assert all(path.exists() for path in (other, invalid_day, business_file, db))


def test_disabled_logs_still_cleanup_expired_metadata(log):
    log._settings_getter = lambda: {"diagnostics": {"enabled": False, "retention_days": 1}}
    expired = _plant(log, "2026-10-27")
    log.write("PHASE_STARTED")
    assert _day(log) == []
    assert log.cleanup_due()["deleted_files"] == 1
    assert not expired.exists()


def test_cleanup_failure_is_nonfatal_and_reported(log, monkeypatch):
    _plant(log, "2026-10-01")
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path.name.startswith("diagnostic-"):
            raise PermissionError("secret-path")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    assert log.cleanup_due()["error"] == "CLEANUP_FAILED"
    assert log.status()["cleanup_error"] == "CLEANUP_FAILED"
    log.write("PHASE_STARTED", phase="identity")
    assert log.status()["write_error"] is None
    assert log.status()["cleanup_error"] == "CLEANUP_FAILED"
    monkeypatch.setattr(Path, "unlink", original)
    assert log.cleanup_due()["performed"] is True
    assert log.status()["cleanup_error"] is None


def _symlink_or_skip(source, target, directory=False):
    try:
        source.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("Symlink creation requires OS permission")


def test_symlink_directory_refused_and_target_untouched(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "diagnostic-2026-10-01.jsonl"
    victim.write_bytes(b"do not modify")
    _symlink_or_skip(state / "diagnostics", outside, directory=True)
    logger = Diagnostics(state, lambda: {})
    logger.write("PHASE_STARTED")
    assert logger.cleanup_due(datetime(2026, 10, 28, 8, 0, tzinfo=module.BEIJING))["performed"] is False
    assert victim.read_bytes() == b"do not modify"
    assert logger.status()["write_error"] == "DIRECTORY_UNAVAILABLE"


def test_symlink_file_is_not_read_written_or_deleted(log, tmp_path):
    log._directory.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"do not modify")
    symbolic = log._directory / "diagnostic-2026-10-28.jsonl"
    _symlink_or_skip(symbolic, outside)
    log.write("PHASE_STARTED")
    assert log.status()["write_error"] == "UNSAFE_FILE"
    assert _day(log) == []
    log.cleanup_due()
    assert symbolic.is_symlink()
    assert outside.read_bytes() == b"do not modify"


def test_hardlink_is_not_read_written_or_deleted(log, tmp_path):
    log._directory.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"do not modify")
    current = log._directory / "diagnostic-2026-10-28.jsonl"
    expired = log._directory / "diagnostic-2026-10-01.jsonl"
    os.link(outside, current)
    os.link(outside, expired)
    log.write("PHASE_STARTED")
    assert log.status()["write_error"] == "UNSAFE_FILE"
    assert _day(log) == []
    assert log.cleanup_due()["deleted_files"] == 0
    assert current.exists() and expired.exists()
    assert outside.read_bytes() == b"do not modify"


def test_unsafe_existing_cleanup_marker_cannot_overwrite_other_file(log, tmp_path):
    log._directory.mkdir(parents=True)
    outside = tmp_path / "outside-marker"
    outside.write_bytes(b"do not modify")
    os.link(outside, log._directory / ".cleanup-state.json")
    assert log.cleanup_due()["error"] == "CLEANUP_FAILED"
    assert outside.read_bytes() == b"do not modify"


def test_naive_cleanup_time_rejected_before_any_cleanup(tmp_path):
    logger = Diagnostics(tmp_path, lambda: {})
    with pytest.raises(ValueError, match="aware datetime"):
        logger.cleanup_due(datetime(2026, 10, 28, 4, 0))
    assert not logger._directory.exists()


def test_concurrent_write_and_cleanup_preserve_valid_json(log):
    _plant(log, "2026-10-01")
    def worker(index):
        if index % 5 == 0:
            log.cleanup_due()
        log.write("PHASE_STARTED", item_count=index)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(100)))
    assert sorted(row["item_count"] for row in _day(log)) == list(range(100))
    assert not (log._directory / "diagnostic-2026-10-01.jsonl").exists()


def test_status_has_only_public_directory_label(log):
    log.write("PHASE_STARTED")
    status = log.status()
    assert status["directory"] == "/state/diagnostics"
    assert status["retention_days"] == 7
    assert status["cleanup_time"] == "03:00"
    assert status["timezone"] == "Asia/Shanghai"
    assert status["available_days"] == ["2026-10-28"]
    assert status["file_count"] == 1
    assert str(log._state) not in json.dumps(status)
