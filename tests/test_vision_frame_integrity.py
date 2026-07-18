from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from live_betting.storage import LiveBettingStore
from live_betting.vision import VisionObservation
from live_betting.vision_frame_registry import (
    VisionFrameReceipt,
    publish_vision_frame_bytes,
    register_vision_frame_artifact,
    verify_registered_vision_frame,
    verify_vision_frame_receipt,
)


NOW = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)


def _observation(receipt: VisionFrameReceipt | None) -> VisionObservation:
    return VisionObservation(
        "match-1",
        1,
        NOW,
        600,
        False,
        (1, 2, 3, 4, 5),
        (6, 7, 8, 9, 10),
        0.95,
        0.95,
        "legacy-frame" if receipt is None else receipt.frame_ref,
        "game",
        "team_one",
        source_frame_sha256=(None if receipt is None else receipt.content_sha256),
        source_frame_bytes=(None if receipt is None else receipt.byte_length),
        source_frame_path=(None if receipt is None else str(receipt.storage_path)),
    )


def test_duplicate_content_is_one_registered_identity(tmp_path: Path) -> None:
    first = publish_vision_frame_bytes(tmp_path, b"frame-pixels")
    second = publish_vision_frame_bytes(tmp_path, b"frame-pixels")
    assert second == first
    assert first.storage_path.stat().st_nlink == 1

    with LiveBettingStore(":memory:") as store:
        store.init_schema()
        assert register_vision_frame_artifact(
            store.connection, first, registered_at=NOW
        )
        assert not register_vision_frame_artifact(
            store.connection, second, registered_at=NOW + timedelta(seconds=1)
        )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM vision_frame_artifacts"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("damage", ["tamper", "truncate", "path_swap"])
def test_registered_frame_rejects_file_damage(tmp_path: Path, damage: str) -> None:
    receipt = publish_vision_frame_bytes(tmp_path, b"original-frame-pixels")
    with LiveBettingStore(":memory:") as store:
        store.init_schema()
        register_vision_frame_artifact(store.connection, receipt, registered_at=NOW)
        if damage == "tamper":
            receipt.storage_path.write_bytes(b"tampered-frame-pixels")
        elif damage == "truncate":
            receipt.storage_path.write_bytes(b"short")
        else:
            replacement = receipt.storage_path.with_name("replacement.jpg")
            replacement.write_bytes(b"replacement-frame")
            os.replace(replacement, receipt.storage_path)
        with pytest.raises(RuntimeError):
            verify_registered_vision_frame(store.connection, receipt.frame_ref)


def test_registered_frame_rejects_external_hardlink_alias(tmp_path: Path) -> None:
    receipt = publish_vision_frame_bytes(tmp_path, b"hardlink-frame")
    alias = tmp_path / "alias.jpg"
    with LiveBettingStore(":memory:") as store:
        store.init_schema()
        register_vision_frame_artifact(store.connection, receipt, registered_at=NOW)
        try:
            os.link(receipt.storage_path, alias)
        except OSError:
            pytest.skip("filesystem does not support hardlinks")
        assert receipt.storage_path.stat().st_nlink == 2
        alias.write_bytes(b"alias-tamper")
        with pytest.raises(RuntimeError):
            verify_registered_vision_frame(store.connection, receipt.frame_ref)


def test_receipt_rejects_hash_and_size_mismatch(tmp_path: Path) -> None:
    receipt = publish_vision_frame_bytes(tmp_path, b"frame-pixels")
    with pytest.raises(RuntimeError, match="receipt"):
        verify_vision_frame_receipt(
            VisionFrameReceipt(
                receipt.frame_ref,
                receipt.content_sha256,
                receipt.byte_length + 1,
                receipt.storage_path,
            )
        )
    with pytest.raises(RuntimeError, match="reference"):
        verify_vision_frame_receipt(
            VisionFrameReceipt(
                f"vision-frame:sha256:{'0' * 64}",
                receipt.content_sha256,
                receipt.byte_length,
                receipt.storage_path,
            )
        )


def test_legacy_frame_is_stored_but_never_trusted(tmp_path: Path) -> None:
    del tmp_path
    with LiveBettingStore(":memory:") as store:
        store.init_schema()
        assert store.insert_vision_observation(_observation(None))
        row = store.connection.execute(
            """SELECT confirmed, source_frame_sha256, source_frame_bytes
                 FROM vision_observations"""
        ).fetchone()
        assert tuple(row) == (0, None, None)
        assert store.connection.execute(
            "SELECT COUNT(*) FROM trusted_vision_observation_authority"
        ).fetchone()[0] == 0


def test_frame_registry_and_observation_identity_are_immutable(
    tmp_path: Path,
) -> None:
    receipt = publish_vision_frame_bytes(tmp_path, b"frame-pixels")
    with LiveBettingStore(":memory:") as store:
        store.init_schema()
        assert store.insert_vision_observation(_observation(receipt))
        with pytest.raises(Exception, match="immutable"):
            store.connection.execute(
                "UPDATE vision_frame_artifacts SET byte_length=byte_length+1"
            )
        with pytest.raises(Exception, match="immutable"):
            store.connection.execute(
                """UPDATE vision_observations
                      SET source_frame_sha256=?""",
                ("0" * 64,),
            )
