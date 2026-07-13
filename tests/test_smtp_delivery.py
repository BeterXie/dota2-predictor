from __future__ import annotations

import smtplib
import unittest
from datetime import datetime, timezone

from live_betting.notifications import (
    EVENT_FILLED,
    OutboxRecord,
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


def record() -> OutboxRecord:
    return OutboxRecord(
        outbox_id=1,
        order_key="order-1",
        event_type=EVENT_FILLED,
        channel="email",
        payload_json='{"raybet_match_id":"match-1","value":1}',
        stats_cutoff_at=NOW,
        template_version="dota2-shadow-email-v1",
        recipient="599084618@qq.com",
        message_id=stable_message_id("order-1", EVENT_FILLED),
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

    def test_message_is_simulation_and_uses_stable_id(self) -> None:
        config = SMTPConfig("sender@qq.com", "secret")
        message = build_message(record(), config)
        self.assertEqual(message["Message-ID"], stable_message_id("order-1", EVENT_FILLED))
        body = message.get_content()
        self.assertIn("SIMULATION ONLY", body)
        self.assertIn("no real wager was placed", body)
        self.assertNotIn("secret", body)

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
