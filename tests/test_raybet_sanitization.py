from __future__ import annotations

from live_betting.sanitize import (
    PUBLIC_STREAM_EVIDENCE_KEY,
    public_stream_evidence,
    sanitize_raybet_payload,
    stored_public_stream_url,
    verified_ephemeral_stream_url,
    verified_public_stream_url,
)


def test_unsigned_public_hls_url_roundtrips_with_writer_provenance() -> None:
    url = "https://play.ehome.gg/live/match.m3u8"
    evidence = public_stream_evidence(url)

    assert verified_public_stream_url(url) == url
    assert stored_public_stream_url(
        url,
        {PUBLIC_STREAM_EVIDENCE_KEY: evidence},
    ) == url


def test_signed_hls_query_and_credentials_never_become_public_capabilities() -> None:
    signed_url = "https://play.ehome.gg/live/match.m3u8?token=secret"

    assert verified_public_stream_url(signed_url) is None
    assert verified_ephemeral_stream_url(signed_url) == signed_url
    assert verified_public_stream_url(
        "https://user:password@play.ehome.gg/live/match.m3u8"
    ) is None
    assert verified_ephemeral_stream_url(
        "https://user:password@play.ehome.gg/live/match.m3u8"
    ) is None


def test_raybet_payload_redacts_tokens_and_signed_url_material() -> None:
    payload = {
        "authorization": "Bearer secret",
        "nested": {
            "stream_url": "https://play.ehome.gg/live/match.m3u8?token=secret",
            "label": "safe",
        },
    }

    sanitized = sanitize_raybet_payload(payload)

    assert "authorization" not in sanitized
    assert sanitized["nested"]["stream_url"] == (
        "https://play.ehome.gg/live/match.m3u8"
    )
    assert sanitized["nested"]["label"] == "safe"
