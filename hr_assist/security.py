from collections import defaultdict, deque
from pathlib import Path
import hashlib
import hmac
import os
import secrets
import threading
import time

from cryptography.fernet import Fernet


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()
    return f"pbkdf2:600000:{salt}:{digest}"


def password_matches(password, stored):
    try:
        return hmac.compare_digest(password_hash(password, stored.split(":")[2]), stored)
    except (IndexError, ValueError):
        return False


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Vault:
    def __init__(self, directory: Path):
        path = directory / "secrets.key"
        if not path.exists():
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(Fernet.generate_key())
        self.cipher = Fernet(path.read_bytes())

    def encrypt(self, text):
        return self.cipher.encrypt(text.encode()).decode() if text else ""

    def decrypt(self, text):
        return self.cipher.decrypt(text.encode()).decode() if text else ""


class LoginLimiter:
    def __init__(self):
        self.entries = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, address):
        with self.lock:
            clock = time.monotonic()
            for key in list(self.entries):
                while self.entries[key] and self.entries[key][0] < clock - 600:
                    self.entries[key].popleft()
                if not self.entries[key]:
                    self.entries.pop(key)
            attempts = self.entries[address]
            if len(attempts) >= 10:
                return False
            attempts.append(clock)
            return True
