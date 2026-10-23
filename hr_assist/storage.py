from pathlib import Path
import hashlib
import os
import re
import zipfile
from io import BytesIO

from .db import now, uid


class BusinessError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return (name[:120] or "未命名")


def classify_filename(name):
    groups = re.findall(r"【([^】]*)】", name)
    if not groups:
        return None, ""
    jobs = {block.split("_", 1)[0].strip() for block in groups}
    if len(jobs) != 1 or not next(iter(jobs)):
        return None, ""
    parts = groups[0].split("_")
    return next(iter(jobs)), parts[1].strip() if len(parts) > 1 else ""


def validate_zip(data):
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            if len(archive.infolist()) > 3000 or sum(i.file_size for i in archive.infolist()) > 60 * 1024 * 1024:
                raise BusinessError("ZIP_LIMIT", "Office 文件解压后过大，已停止处理。")
            if any(i.flag_bits & 1 for i in archive.infolist()):
                raise BusinessError("ENCRYPTED", "Office 文件已加密。")
    except zipfile.BadZipFile as exc:
        raise BusinessError("INVALID_OFFICE", "Office 文件损坏或格式不正确。") from exc


class Storage:
    def __init__(self, root, db):
        self.root, self.db = root.resolve(), db
        for category in ("resumes", "jd", "reports", "followup", "quarantine", "temporary"):
            (self.root / category).mkdir(parents=True, exist_ok=True)

    def path(self, relative):
        result = (self.root / relative).resolve()
        if not result.is_relative_to(self.root):
            raise BusinessError("PATH_INVALID", "文件路径超出数据目录。")
        return result

    def put(self, data, name, kind, folder=None, deduplicate=False):
        digest = hashlib.sha256(data).hexdigest()
        if deduplicate:
            previous = self.db.one("SELECT * FROM files WHERE sha256=? AND kind=?", (digest, kind))
            if previous:
                return previous
        identity = uid()
        category = folder or kind
        relative = f"{category}/{identity}_{safe_filename(name)}"
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self.db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?)",
                        (identity, digest, kind, name, relative, len(data), now()))
        return self.db.one("SELECT * FROM files WHERE id=?", (identity,))

    def file(self, identity):
        row = self.db.one("SELECT * FROM files WHERE id=?", (identity,))
        if not row:
            raise BusinessError("FILE_NOT_FOUND", "文件不存在。", 404)
        return row, self.path(row["relative_path"])
