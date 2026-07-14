from __future__ import annotations

import smtplib
import unittest
from datetime import datetime, timezone

from live_betting.notifications import (
    EVENT_FILLED,
    EVENT_SETTLED,
    OutboxRecord,
    TEMPLATE_VERSION,
    canonical_payload,
    simulation_payload,
    stable_message_id,
)
from live_betting.smtp_delivery import (
    SMTPConfig,
    SMTPConfigurationError,
    build_message,
    classify_error,
    sanitize_header,
    send_message,
)


NOW = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)


def record(
    event_type: str = EVENT_FILLED,
    template_version: str = TEMPLATE_VERSION,
) -> OutboxRecord:
    payload = simulation_payload(event_type, {
        "raybet_match_id": "match-1",
        "map_number": 1,
        "selected_side": "team_one",
        "result": "win",
    })
    payload["template_version"] = template_version
    return OutboxRecord(
        outbox_id=1,
        order_key="order-1",
        event_type=event_type,
        channel="email",
        payload_json=canonical_payload(payload),
        stats_cutoff_at=NOW,
        template_version=template_version,
        recipient="599084618@qq.com",
        message_id=stable_message_id(
            "order-1", event_type, template_version=template_version
        ),
        status="leased",
        attempt_count=1,
        next_attempt_at=NOW,
        lease_token="lease",
        lease_until=NOW,
        last_error=None,
        created_at=NOW,
        sent_at=None,
    )


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host: str, port: int, *, context: object, timeout: float) -> None:
        self.host = host
        self.port = port
        self.context = context
        self.timeout = timeout
        self.logged_in: tuple[str, str] | None = None
        self.message = None
        self.ehlo_called = False
        self.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def ehlo(self) -> None:
        self.ehlo_called = True

    def login(self, sender: str, auth_code: str) -> None:
        self.logged_in = sender, auth_code

    def send_message(self, message: object) -> None:
        self.message = message


class SMTPDeliveryTests(unittest.TestCase):
    def test_missing_configuration_does_not_guess_credentials(self) -> None:
        with self.assertRaises(SMTPConfigurationError):
            SMTPConfig.from_environment({})

    def test_environment_configuration_and_header_sanitization(self) -> None:
        config = SMTPConfig.from_environment(
            {"DOTA2_SMTP_SENDER": "sender@qq.com", "DOTA2_SMTP_AUTH_CODE": "secret"}
        )
        self.assertEqual(config.sender, "sender@qq.com")
        self.assertEqual(sanitize_header("subject\r\ninjected", "subject"), "subject  injected")
        with self.assertRaises(ValueError):
            sanitize_header("bad\x00header", "subject")

    def test_non_ascii_or_whitespace_authorization_code_is_rejected(self) -> None:
        for auth_code in ("\u5df2\u5f55", "two words", "line\nbreak"):
            with self.subTest(auth_code=auth_code):
                with self.assertRaisesRegex(SMTPConfigurationError, "invalid"):
                    SMTPConfig.from_environment({
                        "DOTA2_SMTP_SENDER": "sender@qq.com",
                        "DOTA2_SMTP_AUTH_CODE": auth_code,
                    })

    def test_message_uses_chinese_template_and_stable_id(self) -> None:
        config = SMTPConfig("sender@qq.com", "secret")
        message = build_message(record(), config)
        self.assertEqual(message["Message-ID"], stable_message_id("order-1", EVENT_FILLED))
        self.assertEqual(
            str(message["Subject"]),
            "[Dota 2 模拟] 模拟订单已成交：match-1",
        )
        body = message.get_content()
        self.assertIn("Dota 2 实时模拟通知", body)
        self.assertIn("仅为模拟：未进行任何真实投注。", body)
        self.assertIn("事件：模拟订单已成交", body)
        self.assertIn("RayBet 比赛编号：match-1", body)
        self.assertIn("选择方：队伍一", body)
        self.assertIn("结算结果：赢", body)
        self.assertIn("是否为模拟：是", body)
        self.assertIn("是否真实下注：否", body)
        self.assertIn("模板版本：dota2-shadow-email-v2", body)
        self.assertNotIn("SIMULATION ONLY", body)
        self.assertNotIn("raybet_match_id:", body)
        self.assertNotIn("secret", body)

    def test_settled_message_subject_is_chinese(self) -> None:
        message = build_message(
            record(EVENT_SETTLED),
            SMTPConfig("sender@qq.com", "secret"),
        )
        self.assertEqual(
            str(message["Subject"]),
            "[Dota 2 模拟] 模拟订单已结算：match-1",
        )
        self.assertIn("事件：模拟订单已结算", message.get_content())

    def test_legacy_retry_keeps_legacy_subject_and_body(self) -> None:
        message = build_message(
            record(template_version="dota2-shadow-email-v1"),
            SMTPConfig("sender@qq.com", "secret"),
        )
        self.assertEqual(
            str(message["Subject"]),
            "[Dota2 simulation] filled shadow order: match-1",
        )
        body = message.get_content()
        self.assertIn("SIMULATION ONLY", body)
        self.assertIn("raybet_match_id: match-1", body)
        self.assertNotIn("Dota 2 实时模拟通知", body)

    def test_implicit_tls_delivery_uses_context_and_no_plaintext_client(self) -> None:
        FakeSMTP.instances.clear()
        config = SMTPConfig("sender@qq.com", "auth-code")
        send_message(record(), config, smtp_factory=FakeSMTP)
        client = FakeSMTP.instances[-1]
        self.assertEqual((client.host, client.port), ("smtp.qq.com", 465))
        self.assertTrue(client.ehlo_called)
        self.assertEqual(client.logged_in, ("sender@qq.com", "auth-code"))
        self.assertIsNotNone(client.context)
        self.assertIsNotNone(client.message)

    def test_error_classification_does_not_return_server_text_or_secret(self) -> None:
        transient, reason = classify_error(smtplib.SMTPResponseException(421, b"secret"))
        self.assertTrue(transient)
        self.assertEqual(reason, "smtp_421")
        transient, reason = classify_error(smtplib.SMTPAuthenticationError(535, b"secret"))
        self.assertFalse(transient)
        self.assertEqual(reason, "authentication_failure")


if __name__ == "__main__":
    unittest.main()
