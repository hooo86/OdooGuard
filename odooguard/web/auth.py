"""Authentification locale du tableau de bord (bibliothèque standard uniquement).

* Comptes dans ``/etc/odooguard/web/users`` (600, propriétaire odooguard) :
  une ligne par compte ``nom:rôle:pbkdf2_sha256$<itérations>$<sel b64>$<empreinte b64>``
  (rôles : lecteur, operateur, admin ; une ligne sans rôle, format de la
  phase 2, vaut « lecteur »).
  Gestion : ``odooguard web-user add|role|remove|list``.
* Session : cookie signé HMAC-SHA256 (clé ``/etc/odooguard/web/secret``),
  ``HttpOnly``, ``Secure``, ``SameSite=Strict``, durée limitée.
* Anti-force brute : 5 échecs par adresse IP en 10 min bloquent la connexion
  pendant 10 min.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from threading import Lock

ITERATIONS = 600_000
COOKIE = "odooguard_session"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ------------------------------------------------------------------ mots de passe
def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, it, salt, dk = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(it))
        return hmac.compare_digest(calc, _unb64(dk))
    except (ValueError, TypeError):
        return False


ROLES = ("lecteur", "operateur", "admin")


def read_accounts(path: str) -> dict[str, dict]:
    accounts: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                parts = line.split(":")
                if len(parts) == 2:
                    accounts[parts[0]] = {"role": "lecteur", "hash": parts[1]}
                elif len(parts) == 3 and parts[1] in ROLES:
                    accounts[parts[0]] = {"role": parts[1], "hash": parts[2]}
    except FileNotFoundError:
        pass
    return accounts


def write_accounts(path: str, accounts: dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# Comptes du tableau de bord OdooGuard - géré par « odooguard web-user »\n")
        for name, a in sorted(accounts.items()):
            fh.write(f"{name}:{a['role']}:{a['hash']}\n")
    os.replace(tmp, path)


def read_users(path: str) -> dict[str, str]:
    """Compatibilité : nom -> empreinte."""
    return {n: a["hash"] for n, a in read_accounts(path).items()}


def write_users(path: str, users: dict[str, str], role: str = "lecteur") -> None:
    """Compatibilité : conserve le rôle des comptes existants."""
    current = read_accounts(path)
    write_accounts(path, {n: {"role": current.get(n, {}).get("role", role), "hash": h}
                          for n, h in users.items()})


def csrf_token(secret: bytes, session: str) -> str:
    """Jeton anti-CSRF lié à la session (formulaires de l'assistant)."""
    return _b64(hmac.new(secret, b"csrf|" + session.encode(), hashlib.sha256).digest())


def check_csrf(secret: bytes, session: str | None, token: str | None) -> bool:
    return bool(session and token) and hmac.compare_digest(csrf_token(secret, session), token)


def valid_username(name: str) -> bool:
    return 2 <= len(name) <= 32 and all(c.isalnum() or c in "._-" for c in name)


# ------------------------------------------------------------------ sessions
def load_secret(path: str) -> bytes:
    """Clé de signature ; créée (600) au premier lancement si absente."""
    try:
        with open(path, "rb") as fh:
            key = fh.read().strip()
            if len(key) >= 32:
                return key
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = secrets.token_hex(32).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def make_session(secret: bytes, user: str, hours: float, now: float | None = None) -> str:
    exp = int((now or time.time()) + hours * 3600)
    payload = f"{user}|{exp}|{secrets.token_hex(8)}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
    return f"{_b64(payload.encode())}.{_b64(sig)}"


def read_session(secret: bytes, token: str | None, now: float | None = None) -> str | None:
    if not token or "." not in token:
        return None
    try:
        p64, s64 = token.split(".", 1)
        payload = _unb64(p64)
        expected = hmac.new(secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(s64)):
            return None
        user, exp, _ = payload.decode().split("|")
        if int(exp) < (now or time.time()):
            return None
        return user
    except (ValueError, UnicodeDecodeError):
        return None


class Throttle:
    """Limite les tentatives de connexion par adresse IP."""

    def __init__(self, max_failures: int = 5, window: float = 600):
        self.max = max_failures
        self.window = window
        self.failures: dict[str, list[float]] = {}
        self.lock = Lock()

    def blocked(self, ip: str, now: float | None = None) -> bool:
        now = now or time.time()
        with self.lock:
            recent = [t for t in self.failures.get(ip, []) if now - t < self.window]
            self.failures[ip] = recent
            return len(recent) >= self.max

    def fail(self, ip: str, now: float | None = None) -> None:
        with self.lock:
            self.failures.setdefault(ip, []).append(now or time.time())

    def reset(self, ip: str) -> None:
        with self.lock:
            self.failures.pop(ip, None)
