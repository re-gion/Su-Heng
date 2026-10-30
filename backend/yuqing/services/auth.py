from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlsplit

from yuqing.storage.db import Database

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")
DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$"
)
CODE_RE = re.compile(r"^\d{6}$")
SESSION_COOKIE_NAME = "yuqing_session"
SESSION_TTL_DAYS = 14
CODE_TTL_MINUTES = 10
CODE_MAX_ATTEMPTS = 5
CODE_COOLDOWN_SECONDS = 60
EMAIL_HOURLY_LIMIT = 5
IP_HOURLY_LIMIT = 30


class InvalidEmail(ValueError):
    pass


class InvalidCode(ValueError):
    pass


class CodeRateLimited(ValueError):
    def __init__(self, retry_after_seconds: int):
        super().__init__("verification code rate limit reached")
        self.retry_after_seconds = retry_after_seconds


class MailDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthSettings:
    session_secret: str
    admin_emails: frozenset[str]
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    smtp_password: str
    mail_from: str
    public_origin: str

    @classmethod
    def from_env(cls) -> AuthSettings:
        admin_emails = frozenset(
            email.strip().casefold()
            for email in os.getenv("YUQING_ADMIN_EMAILS", "").split(",")
            if email.strip()
        )
        return cls(
            session_secret=os.getenv("YUQING_AUTH_SESSION_SECRET", ""),
            admin_emails=admin_emails,
            smtp_host=os.getenv("YUQING_SMTP_HOST", "").strip(),
            smtp_port=int(os.getenv("YUQING_SMTP_PORT", "587")),
            smtp_security=os.getenv("YUQING_SMTP_SECURITY", "starttls").strip().lower(),
            smtp_username=os.getenv("YUQING_SMTP_USERNAME", "").strip(),
            smtp_password=os.getenv("YUQING_SMTP_PASSWORD", ""),
            mail_from=os.getenv("YUQING_SMTP_FROM", "").strip(),
            public_origin=os.getenv("YUQING_PUBLIC_ORIGIN", "").strip().rstrip("/"),
        )

    def validate(self) -> None:
        if len(self.session_secret.encode("utf-8")) < 32:
            raise ValueError("YUQING_AUTH_SESSION_SECRET must be at least 32 bytes")
        if not self.admin_emails or any(not valid_email(email) for email in self.admin_emails):
            raise ValueError("YUQING_ADMIN_EMAILS must contain at least one valid email")
        if not self.smtp_host or not self.mail_from or not valid_email(self.mail_from):
            raise ValueError("YUQING_SMTP_HOST and a valid YUQING_SMTP_FROM are required")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("YUQING_SMTP_PORT must be between 1 and 65535")
        if self.smtp_security not in {"starttls", "ssl"}:
            raise ValueError("YUQING_SMTP_SECURITY must be starttls or ssl")
        if bool(self.smtp_username) != bool(self.smtp_password):
            raise ValueError("SMTP username and password must be configured together")
        try:
            parsed = urlsplit(self.public_origin)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("YUQING_PUBLIC_ORIGIN must be a valid https origin") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.netloc.casefold() != parsed.hostname.casefold()
            or port is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("YUQING_PUBLIC_ORIGIN must be an https origin without a path")
        domain = os.getenv("YUQING_DOMAIN", "").strip().casefold()
        if domain:
            if DOMAIN_RE.fullmatch(domain) is None:
                raise ValueError("YUQING_DOMAIN must be a valid hostname")
            if parsed.hostname.casefold() != domain:
                raise ValueError("YUQING_PUBLIC_ORIGIN host must match YUQING_DOMAIN")


def normalize_email(value: str) -> str:
    email = value.strip().casefold()
    if len(email) > 254 or not valid_email(email):
        raise InvalidEmail("请输入有效的邮箱地址。")
    return email


def valid_email(value: str) -> bool:
    return len(value) <= 254 and EMAIL_RE.fullmatch(value) is not None


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


class EmailAuthService:
    def __init__(
        self,
        database: Database,
        settings: AuthSettings,
        *,
        mail_sender: Callable[[str, str], None] | None = None,
    ):
        self.database = database
        self.settings = settings
        self.mail_sender = mail_sender

    @property
    def session_max_age(self) -> int:
        return SESSION_TTL_DAYS * 24 * 60 * 60

    def is_admin(self, email: str) -> bool:
        return email.casefold() in self.settings.admin_emails

    def _key_hash(self, scope: str, value: str) -> str:
        message = f"{scope}:{value}".encode()
        return hmac.new(
            self.settings.session_secret.encode("utf-8"), message, hashlib.sha256
        ).hexdigest()

    def email_rate_limit_key(self, email: str) -> str:
        return self._key_hash("email", normalize_email(email))

    def _code_hash(self, email: str, code: str) -> str:
        message = f"email-code:{email}:{code}".encode()
        return hmac.new(
            self.settings.session_secret.encode("utf-8"), message, hashlib.sha256
        ).hexdigest()

    async def request_code(self, raw_email: str, client_ip: str) -> None:
        email = normalize_email(raw_email)
        now = _utc_now()
        now_text = _iso(now)
        email_key = self._key_hash("email", email)
        ip_key = self._key_hash("ip", client_ip or "unknown")
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_hash = self._code_hash(email, code)

        def operation(connection: sqlite3.Connection) -> int:
            connection.execute("DELETE FROM auth_challenge WHERE expires_at<=?", (now_text,))
            connection.execute("DELETE FROM auth_session WHERE expires_at<=?", (now_text,))
            connection.execute(
                "DELETE FROM auth_rate_limit WHERE window_started_at<=?",
                (_iso(now - timedelta(hours=1)),),
            )
            limits = (
                (email_key, EMAIL_HOURLY_LIMIT, CODE_COOLDOWN_SECONDS),
                (ip_key, IP_HOURLY_LIMIT, 0),
            )
            states: list[tuple[str, str, int, str]] = []
            for key_hash, max_count, cooldown in limits:
                row = connection.execute(
                    "SELECT window_started_at,request_count,last_requested_at "
                    "FROM auth_rate_limit WHERE key_hash=?",
                    (key_hash,),
                ).fetchone()
                if row is None:
                    states.append((key_hash, now_text, 1, now_text))
                    continue
                window_start = datetime.fromisoformat(row["window_started_at"])
                last_requested = datetime.fromisoformat(row["last_requested_at"])
                if now - window_start >= timedelta(hours=1):
                    states.append((key_hash, now_text, 1, now_text))
                    continue
                count = int(row["request_count"])
                if count >= max_count:
                    remaining = max(
                        1, int((window_start + timedelta(hours=1) - now).total_seconds())
                    )
                    return remaining
                if cooldown and now - last_requested < timedelta(seconds=cooldown):
                    remaining = max(1, cooldown - int((now - last_requested).total_seconds()))
                    return remaining
                states.append((key_hash, row["window_started_at"], count + 1, now_text))
            for key_hash, window_start, count, last_requested in states:
                connection.execute(
                    """INSERT INTO auth_rate_limit(key_hash,window_started_at,request_count,last_requested_at)
                       VALUES(?,?,?,?) ON CONFLICT(key_hash) DO UPDATE SET
                       window_started_at=excluded.window_started_at,
                       request_count=excluded.request_count,last_requested_at=excluded.last_requested_at""",
                    (key_hash, window_start, count, last_requested),
                )
            connection.execute(
                """INSERT INTO auth_challenge(email,code_hash,expires_at,attempts,created_at)
                   VALUES(?,?,?,0,?) ON CONFLICT(email) DO UPDATE SET
                   code_hash=excluded.code_hash,expires_at=excluded.expires_at,
                   attempts=0,created_at=excluded.created_at""",
                (email, code_hash, _iso(now + timedelta(minutes=CODE_TTL_MINUTES)), now_text),
            )
            return 0

        retry_after = await self.database.write(operation)
        if retry_after:
            raise CodeRateLimited(retry_after)
        try:
            if self.mail_sender is None:
                await asyncio.to_thread(self._send_email, email, code)
            else:
                await asyncio.to_thread(self.mail_sender, email, code)
        except Exception as exc:
            await self.database.execute_write(
                "DELETE FROM auth_challenge WHERE email=? AND code_hash=?", (email, code_hash)
            )
            raise MailDeliveryError("verification email could not be sent") from exc

    async def verify_code(self, raw_email: str, code: str) -> tuple[str, dict[str, str | bool]]:
        email = normalize_email(raw_email)
        if not CODE_RE.fullmatch(code):
            raise InvalidCode("验证码无效或已过期，请重新获取。")
        now = _utc_now()
        now_text = _iso(now)
        code_hash = self._code_hash(email, code)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()

        def operation(connection: sqlite3.Connection) -> tuple[str, str | None, str | None]:
            challenge = connection.execute(
                "SELECT code_hash,expires_at,attempts FROM auth_challenge WHERE email=?", (email,)
            ).fetchone()
            if challenge is None:
                return "invalid", None, None
            if (
                challenge["expires_at"] <= now_text
                or int(challenge["attempts"]) >= CODE_MAX_ATTEMPTS
            ):
                connection.execute("DELETE FROM auth_challenge WHERE email=?", (email,))
                return "invalid", None, None
            if not hmac.compare_digest(challenge["code_hash"], code_hash):
                attempts = int(challenge["attempts"]) + 1
                if attempts >= CODE_MAX_ATTEMPTS:
                    connection.execute("DELETE FROM auth_challenge WHERE email=?", (email,))
                else:
                    connection.execute(
                        "UPDATE auth_challenge SET attempts=? WHERE email=?", (attempts, email)
                    )
                return "invalid", None, None

            connection.execute("DELETE FROM auth_challenge WHERE email=?", (email,))
            user = connection.execute(
                "SELECT id,email FROM user_account WHERE email=? COLLATE NOCASE", (email,)
            ).fetchone()
            if user is None:
                user_id = f"u_{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO user_account(id,email,created_at) VALUES(?,?,?)",
                    (user_id, email, now_text),
                )
            else:
                user_id = str(user["id"])
            connection.execute(
                "INSERT INTO auth_session(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (
                    token_hash,
                    user_id,
                    _iso(now + timedelta(days=SESSION_TTL_DAYS)),
                    now_text,
                ),
            )
            return "ok", user_id, email

        status, user_id, verified_email = await self.database.write(operation)
        if status != "ok" or user_id is None or verified_email is None:
            raise InvalidCode("验证码无效或已过期，请重新获取。")
        return token, {
            "id": user_id,
            "email": verified_email,
            "is_admin": self.is_admin(verified_email),
        }

    async def user_for_token(self, token: str | None) -> dict[str, str | bool] | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        now_text = _iso(_utc_now())
        row = await self.database.fetch_one(
            """SELECT u.id,u.email FROM auth_session s
               JOIN user_account u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?""",
            (token_hash, now_text),
        )
        if row is None:
            return None
        email = str(row["email"])
        return {"id": str(row["id"]), "email": email, "is_admin": self.is_admin(email)}

    async def revoke_token(self, token: str | None) -> None:
        if not token:
            return
        token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        await self.database.execute_write(
            "DELETE FROM auth_session WHERE token_hash=?", (token_hash,)
        )

    def _send_email(self, email: str, code: str) -> None:
        message = EmailMessage()
        message["Subject"] = "舆情专报 Agent 登录验证码"
        message["From"] = self.settings.mail_from
        message["To"] = email
        message.set_content(
            f"你的登录验证码是：{code}\n\n验证码 10 分钟内有效。若非本人操作，请忽略此邮件。"
        )
        context = ssl.create_default_context()
        if self.settings.smtp_security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=15,
                context=context,
            )
        else:
            client = smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=15)
        with client:
            if self.settings.smtp_security == "starttls":
                client.starttls(context=context)
            if self.settings.smtp_username:
                client.login(self.settings.smtp_username, self.settings.smtp_password)
            client.send_message(message)
