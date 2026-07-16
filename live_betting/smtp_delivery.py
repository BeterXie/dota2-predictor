"""Verified implicit-TLS SMTP delivery for the simulation outbox."""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Mapping

from .notifications import (
    EVENT_FILLED,
    EVENT_MONITOR_ALERT,
    EVENT_SETTLED,
    MONITOR_TEMPLATE_VERSION,
    OutboxRecord,
    TEMPLATE_VERSION,
)


SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

_EVENT_LABELS = {
    EVENT_FILLED: "模拟订单已成交",
    EVENT_SETTLED: "模拟订单已结算",
}
_PAYLOAD_LABELS = {
    "raybet_match_id": "RayBet 比赛编号",
    "map_number": "地图局号",
    "selected_side": "选择方",
    "signal_price": "信号赔率",
    "fill_price": "成交赔率",
    "model_probability": "模型概率",
    "market_probability": "市场隐含概率",
    "edge": "模型优势",
    "signal_transport_at": "信号传输时间",
    "filled_at": "成交时间",
    "order_key": "订单编号",
    "result": "结算结果",
    "return_units": "返还单位",
    "evidence_ref": "结算依据",
    "settled_at": "结算时间",
    "simulation": "是否为模拟",
    "real_wager_placed": "是否真实下注",
    "event_type": "事件类型",
    "template_version": "模板版本",
}
_SIDE_LABELS = {
    "team_one": "队伍一",
    "team_two": "队伍二",
}
_RESULT_LABELS = {
    "win": "赢",
    "half_win": "赢一半",
    "push": "走盘",
    "half_loss": "输一半",
    "loss": "输",
}
_LEGACY_TEMPLATE_VERSION = "dota2-shadow-email-v1"
_CURRENT_TEMPLATE_VERSION = TEMPLATE_VERSION


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
        if any(ord(char) < 33 or ord(char) > 126 for char in auth_code):
            raise SMTPConfigurationError("SMTP authorization code is invalid")
        _validate_header(sender, "sender")
        return cls(sender=sender, auth_code=auth_code)


def build_message(record: OutboxRecord, config: SMTPConfig) -> EmailMessage:
    payload = record.payload
    if record.template_version == MONITOR_TEMPLATE_VERSION:
        event_label = (
            "监控告警" if record.event_type == EVENT_MONITOR_ALERT else "监控恢复"
        )
        subject_text = f"[Dota 2 监控] {event_label}："
    elif record.template_version == _LEGACY_TEMPLATE_VERSION:
        event_label = (
            "filled shadow order"
            if record.event_type == EVENT_FILLED
            else "settled shadow order"
        )
        subject_text = f"[Dota2 simulation] {event_label}: "
    elif record.template_version == _CURRENT_TEMPLATE_VERSION:
        event_label = _EVENT_LABELS[record.event_type]
        subject_text = f"[Dota 2 模拟] {event_label}："
    else:
        raise ValueError(f"unsupported SMTP template: {record.template_version}")
    match = _payload_text(
        payload,
        "raybet_match_id",
        _payload_text(payload, "title", "monitor"),
    )
    subject = sanitize_header(f"{subject_text}{match}", "subject")
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
    if record.template_version == MONITOR_TEMPLATE_VERSION:
        event_label = (
            "监控告警" if record.event_type == EVENT_MONITOR_ALERT else "监控恢复"
        )
        source = payload.get("source", {})
        return "\n".join(
            (
                "Dota 2 本机监控通知",
                f"事件：{event_label}",
                f"级别：{payload.get('severity', 'unknown')}",
                f"标题：{payload.get('title', '')}",
                f"详情：{payload.get('body', '')}",
                f"来源：{source}",
                f"事件编号：{payload.get('incident_id', '')}",
                f"统计数据截止时间：{record.stats_cutoff_at.isoformat()}",
                "",
            )
        )
    if record.template_version == _LEGACY_TEMPLATE_VERSION:
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
    if record.template_version != _CURRENT_TEMPLATE_VERSION:
        raise ValueError(f"unsupported SMTP template: {record.template_version}")
    lines = [
        "Dota 2 实时模拟通知",
        "仅为模拟：未进行任何真实投注。",
        f"事件：{_EVENT_LABELS[record.event_type]}",
        f"订单：{record.order_key}",
        f"统计数据截止时间：{record.stats_cutoff_at.isoformat()}",
        "",
    ]
    for key in sorted(payload):
        label = _PAYLOAD_LABELS.get(key, f"其他信息（{key}）")
        lines.append(f"{label}：{_display_value(key, payload[key])}")
    return "\n".join(lines) + "\n"


def _display_value(key: str, value: object) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "无"
    if key == "event_type":
        return _EVENT_LABELS.get(str(value), str(value))
    if key == "selected_side":
        return _SIDE_LABELS.get(str(value), str(value))
    if key == "result":
        return _RESULT_LABELS.get(str(value), str(value))
    return str(value)


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
