"""Bounded, metadata-only troubleshooting logs, separate from the audit trail.

The allowlist is intentionally applied on both write and read. Never add prompt,
response, exception-message, endpoint, filename or candidate-name fields here.
"""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from .storage import BusinessError


BEIJING = ZoneInfo("Asia/Shanghai")
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES_PER_DAY = 10
MAX_EXPORT_BYTES = 10 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024
PHASES = frozenset({"preflight", "internal", "archive", "mail_fetch", "document_parse", "identity", "assessment", "report_export",
                    "jd_text", "jd_import", "identity_review"})
EVENTS = frozenset({"PHASE_STARTED", "PHASE_COMPLETED", "PHASE_FAILED", "MODEL_REQUEST_STARTED",
                    "MODEL_HEADERS_RECEIVED", "MODEL_RESPONSE_RECEIVED", "MODEL_REQUEST_COMPLETED",
                    "MODEL_REQUEST_FAILED"})
ERROR_CODES = frozenset("""
MODEL_NOT_CONFIGURED MODEL_FAILED MODEL_TIMEOUT MODEL_CONNECT MODEL_TRANSPORT MODEL_AUTH
MODEL_FORBIDDEN MODEL_RATE_LIMIT MODEL_ENDPOINT MODEL_REQUEST MODEL_UNAVAILABLE MODEL_HTTP
MODEL_RESPONSE_FORMAT MODEL_EMPTY_CONTENT MODEL_TRUNCATED MODEL_INVALID_JSON MODEL_OUTPUT_LIMIT
MODEL_REFUSED MODEL_SCHEMA MODEL_CRITERIA EVIDENCE_INVALID EVIDENCE_MISSING FACT_INVALID
FACT_CONFLICT FACT_EVIDENCE_INVALID FACT_VALUE_MISMATCH NAME_REVIEW_REQUIRED POLICY_UNCONFIRMED
EMPTY_DOCUMENT EMPTY_PAGE INVALID_PDF OCR_FAILED OCR_UNAVAILABLE PAGE_LIMIT PARSE_FAILED
PARSE_TIMEOUT PDF_ENCRYPTED TEXT_LIMIT UNSUPPORTED JD_ANALYSIS_BUSY JD_EVIDENCE_INVALID
JD_FIELD_LIMIT JD_FORMAT JD_IMPORT_NOT_FOUND JD_IMPORT_STATE JD_IMPORT_TARGET JD_INTERRUPTED
JD_MODEL_SCHEMA JD_MULTIPLE_ROLES JD_NAME_INVALID JD_QUEUE_FULL JD_TEXT_INVALID JD_IMPORT_FAILED
JD_VERSION_CONFLICT UPLOAD_LIMIT MAIL_CONNECT MAIL_DATE MAIL_FETCH MAIL_SEARCH
CELL_LIMIT FEEDBACK_CONFLICT ROW_ID_INVALID ROW_REMOVED UNKNOWN_EXPORT WORKBOOK_INVALID
APPLICATION_NOT_FOUND ARCHIVE_HASH DOCUMENT_MISSING DUPLICATE_APPLICATION JD_FILE JD_INCOMPLETE
NAME_CHANGED PREFLIGHT RESUME_FORMAT ROLE_ALREADY_ASSIGNED ROLE_DUPLICATE ROLE_IN_USE
ROLE_INACTIVE ROLE_NOT_FOUND RUN_BUSY ENCRYPTED FILE_NOT_FOUND INVALID_OFFICE PATH_INVALID
ZIP_LIMIT IDENTITY_SCHEMA IDENTITY_EVIDENCE_INVALID IDENTITY_FAILED INTERNAL_ERROR
MODEL_BUDGET SHUTDOWN WAITING_CONFIG WAITING_INPUT IDENTITY_AMBIGUOUS IDENTITY_REVIEW_REQUIRED
""".split())
EXCEPTION_TYPES = frozenset("""
BusinessError ValueError TimeoutError ReadTimeout ConnectTimeout WriteTimeout PoolTimeout
TimeoutException HTTPStatusError ConnectError ReadError WriteError RemoteProtocolError
LocalProtocolError ProtocolError RequestError TransportError NetworkError JSONDecodeError
ValidationError OSError IOError PermissionError FileNotFoundError RuntimeError KeyError
TypeError UnicodeDecodeError EOFError Exception OtherException HTTPError InvalidURL IndexError
AttributeError UnexpectedError
""".split())
ID_FIELDS = frozenset({"run_id", "application_id", "jd_import_id", "file_id", "trace_id", "request_id"})
NUMERIC_FIELDS = frozenset("""
input_characters request_bytes system_characters payload_characters response_bytes header_wait_ms
first_byte_ms elapsed_ms read_timeout_seconds connect_timeout_seconds http_status span_count
content_characters reasoning_characters prompt_tokens completion_tokens total_tokens reasoning_tokens
cached_tokens input_tokens output_tokens attempt retry_count item_count candidate_count criterion_count
""".split())
_FILE = re.compile(r"^diagnostic-(\d{4}-\d{2}-\d{2})(?:\.(0[1-9]))?\.jsonl$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UUID = re.compile(r"^[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
_CONTEXT = ContextVar("hr_diagnostic_context", default=None)


def _valid_day(value):
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(BEIJING).isoformat(timespec="milliseconds")
    except (ValueError, OverflowError):
        return None


def _sanitize(fields):
    result = {}
    for key, value in fields.items():
        if key in ID_FIELDS and isinstance(value, str) and _UUID.fullmatch(value):
            result[key] = value
        elif key in NUMERIC_FIELDS and type(value) in (int, float):
            if 0 <= value <= 10**12 and (type(value) is int or math.isfinite(value)):
                if key != "http_status" or (type(value) is int and 100 <= value <= 599):
                    result[key] = value
        elif key == "phase" and isinstance(value, str) and value in PHASES:
            result[key] = value
        elif key == "provider" and isinstance(value, str) and value in {"zhipu", "other"}:
            result[key] = value
        elif key == "reasoning_effort" and isinstance(value, str) and value in {"default", "low", "high", "max"}:
            result[key] = value
        elif key == "max_tokens" and type(value) is int and 1024 <= value <= 131072:
            result[key] = value
        elif key == "model_fingerprint" and isinstance(value, str) and _HASH.fullmatch(value):
            result[key] = value
        elif key == "error_code" and isinstance(value, str) and value in ERROR_CODES:
            result[key] = value
        elif key == "exception_type" and isinstance(value, str) and value in EXCEPTION_TYPES:
            result[key] = value
        elif key == "finish_reason" and isinstance(value, str) and value in {
                "stop", "length", "content_filter", "sensitive", "tool_calls", "function_call", "unknown", "missing", "other", "none"}:
            result[key] = value
        elif key == "stack" and isinstance(value, list):
            frames = []
            for frame in value[-12:]:
                if not isinstance(frame, dict):
                    continue
                filename, function, line = frame.get("file"), frame.get("function"), frame.get("line")
                # Only code basenames and identifiers; never traceback text or source lines.
                if (isinstance(filename, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\.py", filename)
                        and isinstance(function, str) and (_IDENTIFIER.fullmatch(function) or function == "<module>")
                        and type(line) is int and 0 < line < 1000000):
                    frames.append({"file": filename, "function": function, "line": line})
            if frames:
                result[key] = frames
    return result


def _failure(exc):
    exception_type = type(exc).__name__
    code = getattr(exc, "code", None)
    result = {"exception_type": exception_type if exception_type in EXCEPTION_TYPES else "OtherException",
              "error_code": code if isinstance(code, str) and code in ERROR_CODES else "INTERNAL_ERROR"}
    frames, tb = [], exc.__traceback__
    while tb is not None:
        code_object = tb.tb_frame.f_code
        frames.append({"file": Path(code_object.co_filename).name,
                       "function": code_object.co_name, "line": tb.tb_lineno})
        tb = tb.tb_next
    result["stack"] = frames[-12:]
    return _sanitize(result)


def emit(event, **fields):
    """Emit transport metadata into the current operation; no global fallback."""
    context = _CONTEXT.get()
    if context is not None:
        logger, bound = context
        logger.write(event, **{**fields, **bound})


class Diagnostics:
    def __init__(self, state_dir, settings_getter):
        self._state = Path(state_dir).resolve()
        self._directory = self._state / "diagnostics"
        self._settings_getter = settings_getter
        self._lock = threading.RLock()
        self._write_error = None
        self._cleanup_error = None
        self._capped_day = None
        self._last_cleanup = None
        self._load_cleanup_marker()

    def _settings(self):
        try:
            config = self._settings_getter().get("diagnostics", {})
            if not isinstance(config, dict):
                config = {}
        except Exception:
            config = {}
        days = config.get("retention_days", 7)
        if type(days) is not int or not 1 <= days <= 30:
            days = 7
        cleanup = config.get("cleanup_time", "03:00")
        if not isinstance(cleanup, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", cleanup):
            cleanup = "03:00"
        return {"enabled": config.get("enabled", True) is True, "retention_days": days,
                "cleanup_time": cleanup, "timezone": "Asia/Shanghai"}

    @staticmethod
    def _at(at=None):
        if at is None:
            return datetime.now(BEIJING)
        if not isinstance(at, datetime) or at.tzinfo is None:
            raise ValueError("An aware datetime is required")
        return at.astimezone(BEIJING)

    def _folder(self, create=False):
        if self._directory.is_symlink() or getattr(self._directory, "is_junction", lambda: False)():
            raise OSError("Unsafe diagnostic directory")
        if create:
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self._directory.exists():
            return False
        if not self._directory.is_dir() or self._directory.resolve() != self._directory:
            raise OSError("Unsafe diagnostic directory")
        if create and os.name != "nt":
            os.chmod(self._directory, 0o700)
        return True

    def _ordinary(self, path):
        if path.parent != self._directory or path.is_symlink():
            return False
        try:
            info = path.lstat()
            return stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        except FileNotFoundError:
            return False

    def _open(self, path, *, append=False, truncate=False):
        self._folder(create=True)
        if path.parent != self._directory or (path.exists() and not self._ordinary(path)) or path.is_symlink():
            raise OSError("Unsafe diagnostic file")
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        flags |= os.O_APPEND if append else 0
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("Unsafe diagnostic file")
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            if truncate:
                os.ftruncate(fd, 0)
            return os.fdopen(fd, "ab" if append else "wb")
        except BaseException:
            os.close(fd)
            raise

    def _reader(self, path):
        if not self._folder() or not self._ordinary(path):
            raise OSError("Unsafe diagnostic file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("Unsafe diagnostic file")
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    def _files(self, day=None):
        if not self._folder():
            return []
        files = []
        for path in self._directory.iterdir():
            match = _FILE.fullmatch(path.name)
            if (match and _valid_day(match[1]) and self._ordinary(path)
                    and (day is None or match[1] == day)):
                files.append((match[1], int(match[2] or 0), path))
        return sorted(files, key=lambda item: (item[0], item[1]))

    def write(self, event, **fields):
        if not isinstance(event, str) or event not in EVENTS:
            return
        try:
            if not self._settings()["enabled"]:
                return
            at = self._at()
            day = at.date().isoformat()
            record = {"timestamp": at.isoformat(timespec="milliseconds"), "event": event, **_sanitize(fields)}
            data = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
            if len(data) > MAX_RECORD_BYTES or len(data) > MAX_FILE_BYTES:
                self._write_error = "RECORD_LIMIT"
                return
            with self._lock:
                self._folder(create=True)
                for number in range(MAX_FILES_PER_DAY):
                    suffix = f".{number:02d}" if number else ""
                    path = self._directory / f"diagnostic-{day}{suffix}.jsonl"
                    if path.exists() or path.is_symlink():
                        if not self._ordinary(path):
                            self._write_error = "UNSAFE_FILE"
                            return
                        if path.stat().st_size + len(data) > MAX_FILE_BYTES:
                            continue
                    with self._open(path, append=True) as stream:
                        stream.write(data)
                    self._write_error = None
                    return
                self._capped_day = day
                self._write_error = "DAY_CAP_REACHED"
        except Exception:
            self._write_error = "WRITE_FAILED"

    @contextmanager
    def operation(self, phase, **fields):
        # Rejecting an unknown phase would interfere with the business operation.
        if not isinstance(phase, str) or phase not in PHASES:
            yield None
            return
        context = _CONTEXT.get()
        inherited = context[1] if context is not None and context[0] is self else {}
        bound = {**inherited, **_sanitize(fields), "phase": phase, "trace_id": uuid.uuid4().hex}
        token = _CONTEXT.set((self, bound))
        started = time.monotonic()
        self.write("PHASE_STARTED", **bound)
        try:
            yield bound["trace_id"]
        except BaseException as exc:
            try:
                failure = _failure(exc)
            except Exception:
                failure = {"error_code": "INTERNAL_ERROR", "exception_type": "OtherException"}
            self.write("PHASE_FAILED", **{**bound, **failure, "elapsed_ms": round((time.monotonic() - started) * 1000, 3)})
            raise
        else:
            self.write("PHASE_COMPLETED", **{**bound, "elapsed_ms": round((time.monotonic() - started) * 1000, 3)})
        finally:
            _CONTEXT.reset(token)

    def failure(self, phase, exc, **fields):
        """Record a boundary failure without exposing the exception text."""
        if not isinstance(phase, str) or phase not in PHASES:
            return
        try:
            context = _CONTEXT.get()
            inherited = context[1] if context is not None and context[0] is self else {}
            bound = {**inherited, **_sanitize(fields), "phase": phase}
            if "trace_id" not in bound:
                bound["trace_id"] = uuid.uuid4().hex
            self.write("PHASE_FAILED", **{**bound, **_failure(exc)})
        except Exception:
            self._write_error = "WRITE_FAILED"

    def _query(self, day, run_id):
        if not _valid_day(day) or (run_id is not None and (not isinstance(run_id, str) or not _UUID.fullmatch(run_id))):
            raise BusinessError("DIAGNOSTIC_QUERY", "诊断日期或批次编号格式无效。")

    def _records(self, day, run_id):
        for _, _, path in self._files(day):
            # Refuse oversized files placed by something other than this logger.
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            with self._reader(path) as stream:
                while True:
                    line = stream.readline(MAX_RECORD_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_RECORD_BYTES:
                        while line and not line.endswith(b"\n"):
                            line = stream.readline(MAX_RECORD_BYTES + 1)
                        continue
                    try:
                        raw = json.loads(line)
                        if not isinstance(raw, dict) or raw.get("event") not in EVENTS:
                            continue
                        timestamp = _timestamp(raw.get("timestamp"))
                        if timestamp is None:
                            continue
                        clean = {"timestamp": timestamp, "event": raw["event"], **_sanitize(raw)}
                        if run_id is None or clean.get("run_id") == run_id:
                            yield clean
                    except (ValueError, TypeError, UnicodeError):
                        continue

    def read(self, day, limit=200, run_id=None):
        self._query(day, run_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise BusinessError("DIAGNOSTIC_QUERY", "诊断读取条数应为 1 至 1000。")
        with self._lock:
            try:
                return list(deque(self._records(day, run_id), maxlen=limit))
            except OSError as exc:
                raise BusinessError("DIAGNOSTIC_READ", "无法读取诊断日志，请检查日志目录权限。") from exc

    def export(self, day, run_id=None):
        self._query(day, run_id)
        chunks, size = [], 0
        with self._lock:
            try:
                for record in self._records(day, run_id):
                    data = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
                    size += len(data)
                    if size > MAX_EXPORT_BYTES:
                        raise BusinessError("DIAGNOSTIC_EXPORT_LIMIT", "当天诊断日志超过导出限制，请按批次筛选。")
                    chunks.append(data)
            except OSError as exc:
                raise BusinessError("DIAGNOSTIC_READ", "无法读取诊断日志，请检查日志目录权限。") from exc
        return b"".join(chunks)

    def status(self):
        with self._lock:
            result = {**self._settings(), "directory": "/state/diagnostics", "available_days": [], "days": [],
                      "file_count": 0, "write_error": self._write_error, "cleanup_error": self._cleanup_error,
                      "last_cleanup": self._last_cleanup,
                      "daily_capped": self._capped_day == self._at().date().isoformat()}
            try:
                files = self._files()
                result["available_days"] = result["days"] = sorted({item[0] for item in files}, reverse=True)
                result["file_count"] = len(files)
            except OSError:
                result["write_error"] = "DIRECTORY_UNAVAILABLE"
            return result

    def _load_cleanup_marker(self):
        try:
            path = self._directory / ".cleanup-state.json"
            if self._folder() and self._ordinary(path) and path.stat().st_size <= 1024:
                with self._reader(path) as stream:
                    marker = json.loads(stream.read(1025))
                if isinstance(marker, dict) and marker.get("version") == 1 and _valid_day(marker.get("day")):
                    self._last_cleanup = marker["day"]
        except (OSError, ValueError, TypeError):
            pass

    def cleanup_due(self, at=None):
        """Remove expired diagnostic files once per Beijing date, including startup catch-up."""
        current = self._at(at)
        settings = self._settings()
        today = current.date().isoformat()
        with self._lock:
            if current.strftime("%H:%M") < settings["cleanup_time"] or self._last_cleanup == today:
                return {"performed": False, "deleted_files": 0, "last_cleanup": self._last_cleanup}
            deleted = 0
            try:
                cutoff = (current.date() - timedelta(days=settings["retention_days"] - 1)).isoformat()
                for day, _, path in self._files():
                    if day < cutoff and self._ordinary(path):
                        path.unlink()
                        deleted += 1
                marker = {"version": 1, "day": today}
                target = self._directory / ".cleanup-state.json"
                temporary = self._directory / ".cleanup-state.tmp"
                if target.is_symlink() or (target.exists() and not self._ordinary(target)):
                    raise OSError("Unsafe cleanup marker")
                with self._open(temporary, truncate=True) as stream:
                    stream.write(json.dumps(marker, separators=(",", ":")).encode("ascii"))
                os.replace(temporary, target)
                self._last_cleanup = today
                self._cleanup_error = None
                return {"performed": True, "deleted_files": deleted, "last_cleanup": today}
            except OSError:
                self._cleanup_error = "CLEANUP_FAILED"
                return {"performed": False, "deleted_files": deleted, "last_cleanup": self._last_cleanup,
                        "error": "CLEANUP_FAILED"}
