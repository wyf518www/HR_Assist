from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Environment:
    state_dir: Path
    data_dir: Path
    admin_username: str
    admin_password: str
    secure_cookie: bool
    scheduler: bool
    public_url: str
    max_upload_bytes: int = 20 * 1024 * 1024
    session_hours: int = 8

    @classmethod
    def read(cls):
        password = os.getenv("HR_ADMIN_PASSWORD", "")
        if os.getenv("HR_ADMIN_PASSWORD_FILE"):
            password = Path(os.environ["HR_ADMIN_PASSWORD_FILE"]).read_text(encoding="utf-8").strip()
        return cls(
            Path(os.getenv("HR_STATE_DIR", "runtime/state")).resolve(),
            Path(os.getenv("HR_DATA_DIR", "runtime/data")).resolve(),
            os.getenv("HR_ADMIN_USERNAME", "admin"), password,
            os.getenv("HR_SECURE_COOKIE", "true").lower() == "true",
            os.getenv("HR_SCHEDULER", "true").lower() == "true",
            os.getenv("HR_PUBLIC_URL", "").rstrip("/"),
        )


DEFAULT_SETTINGS = {
    "mail": {"account": "", "host": "imap.qq.com", "port": 993,
             "keywords": ["【BOSS直聘】", "应聘"], "match_mode": "all", "enabled": False},
    "llm": {"endpoint": "", "model": "", "approved": False, "approval_note": "",
            "max_calls_per_run": 30, "timeout_seconds": 300,
            "reasoning_effort": "default", "max_tokens": None},
    "schedule": {"enabled": False, "time": "09:00", "timezone": "Asia/Shanghai"},
    "diagnostics": {"enabled": True, "retention_days": 7, "cleanup_time": "03:00"},
    "policy": {"confirmed": False, "master_bypass": True, "schools": [], "school_source": ""},
}
