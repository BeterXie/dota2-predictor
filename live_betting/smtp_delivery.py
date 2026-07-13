"""Verified implicit-TLS SMTP delivery for the simulation outbox."""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Mapping

from .notifications import EVENT_FILLED, OutboxRecord


SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465


class SMTPConfigurationError(RuntimeError):
    """Required sender configuration is unavailable."""


@dataclass(frozen=True)
class SMTPConfig:
    sender: str
    auth_code: str
    host: str = SMTP_HOST
    port: int = SMTP_PORT
    timeout: float = 20.0

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "SMTPConfig":
        env = os.environ if environment is None else environment
        sender = str(env.get("DOTA2_SMTP_SENDER", "")).strip()
        auth_code = str(env.get("DOTA2_SMTP_AUTH_CODE", ""))
        if not auth_code and sender:
            auth_code = _read_keyring(sender)
        if not sender or not auth_code:
            raise SMTPConfigurationError("SMTP sender or authorization code is missing")
        _validate_header(sender, "sender")
        return cls(sender=sender, auth_code=auth_code)


def build_message(record: OutboxRecord, config: SMTPConfig) -> EmailMessage:
    payload = record.payload
    event_label = "filled shadow order" if record.event_type == EVENT_FILLED else "settled shadow order"
    match = _payload_text(payload, "raybet_match_id", "unknown-match")
    subject = sanitize_header(f"[Dota2 simulation] {event_label}: {match}", "subject")
    message = EmailMessage()
    message["From"] = sanitize_header(config.sender, "sender")
    message["To"] = sanitize_header(record.recipient, "recipient")
    message["Subject"] = subject
    message["Message-ID"] = sanitize_header(record.message_id, "message-id")
    message.set_content(render_body(record))
    return message


def render_body(record: OutboxRecord) -> str:
    """Render only immutable stored payload; retries cannot change statistics."""
    payload = record.payload
    lines = [
        "Dota 2 live shadow notification",
        "SIMULATION ONLY: no real wager was placed.",
        f"Event: {record.event_type}",
        f"Order: {record.order_key}",
        f"Statistics cutoff: {record.stats_cutoff_at.isoformat()}",
        "",
    ]
    for key in sorted(payload):
        lines.append(f"{key}: {payload[key]}")
    return "\n".join(lines) + "\n"


def send_message(
    record: OutboxRecord,
    config: SMTPConfig,
    *,
    smtp_factory: object | None = None,
) -> None:
    message = build_message(record, config)
    factory = smtp_factory or smtplib.SMTP_SSL
    context = ssl.create_default_context()
    with factory(
        config.host,
        config.port,
        context=context,
        timeout=config.timeout,
    ) as client:
        client.ehlo()
        client.login(config.sender, config.auth_code)
        client.send_message(message)


def classify_error(error: BaseException) -> tuple[bool, str]:
    """Return (transient, redacted reason) without retaining SMTP secrets."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return False, "authentication_failure"
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return False, "invalid_recipient"
    if isinstance(error, smtplib.SMTPResponseException):
        code = int(error.smtp_code)
        return (400 <= code < 500), f"smtp_{code}"
    if isinstance(error, (smtplib.SMTPServerDisconnected, TimeoutError, OSError)):
        return True, "network_failure"
    return True, "delivery_failure"


def sanitize_header(value: str, field: str) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if not text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"invalid {field} header")
    return text


def _read_keyring(sender: str) -> str:
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        return ""
    try:
        return str(keyring.get_password("dota2-shadow-smtp", sender) or "")
    except Exception:
        return ""


def _validate_header(value: str, field: str) -> None:
    sanitize_header(value, field)


def _payload_text(payload: dict[str, object], key: str, default: str) -> str:
    value = payload.get(key, default)
    return sanitize_header(str(value), key)


__all__ = [
    "SMTPConfig",
    "SMTPConfigurationError",
    "build_message",
    "classify_error",
    "render_body",
    "sanitize_header",
    "send_message",
]
