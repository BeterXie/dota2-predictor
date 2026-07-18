from __future__ import annotations

import sqlite3
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from live_betting.browser_contract import (
    BrowserEvent,
    EventType,
    Transport,
    canonical_json,
    payload_sha256,
)
from live_betting.browser_ingest import BrowserEventIngestor
from live_betting.markets import (
    legacy_normalized_state_hash_v1,
    normalized_state_hash,
    snapshots_from_payload,
)
from live_betting.odds_response_authority import (
    canonical_state_outcomes,
    normalized_state_identity,
    response_artifact_identity,
    response_state_identity,
    snapshot_derived_payload,
)
from live_betting.odds_response_verifier import verify_odds_response_authority
from live_betting.storage import LiveBettingStore
from event_intelligence.raw_archive import RawArchive


NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)


def odds_payload(*, request_sequence: int | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "result": {
            "id": "1001",
            "game_id": 151,
            "team": [
                {"team_id": 10, "team_name": "One", "pos": 1},
                {"team_id": 20, "team_name": "Two", "pos": 2},
            ],
            "odds": [
                {
                    "id": "winner-one",
                    "odds_group_id": "winner",
                    "team_id": 10,
                    "match_stage": "r1",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "odds": "2.10",
                    "status": 1,
                    "last_update": "stable-provider-state",
                },
                {
                    "id": "winner-two",
                    "odds_group_id": "winner",
                    "team_id": 20,
                    "match_stage": "r1",
                    "group_short_name": "Winner",
                    "tag": "win",
                    "odds": "1.80",
                    "status": 1,
                    "last_update": "stable-provider-state",
                },
            ],
        }
    }
    if request_sequence is not None:
        payload["request_sequence"] = request_sequence
    return payload


def browser_event(index: int, payload: dict[str, object]) -> BrowserEvent:
    captured_at = NOW + timedelta(seconds=index)
    return BrowserEvent.model_validate(
        {
            "schema_version": 1,
            "event_id": f"{index + 1:064x}",
            "capture_session_id": "a" * 32,
            "captured_at_utc": captured_at,
            "page_origin": "https://www.ray086.com",
            "page_path": "/esports",
            "source_path": "/v2/odds",
            "transport": Transport.XHR,
            "event_type": EventType.ODDS,
            "raybet_match_id": "1001",
            "game_id": 151,
            "payload": payload,
            "payload_hash": payload_sha256(payload),
            "payload_bytes": len(canonical_json(payload)),
            "capture_reason": None,
            "extension_version": "0.1.0",
        }
    )


def scalar(store: LiveBettingStore, sql: str) -> int:
    return int(store.connection.execute(sql).fetchone()[0])


def test_shared_authority_verifier_recomputes_state_transport_and_raw(
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "raw"
    payload = odds_payload()
    rows = snapshots_from_payload(payload, received_at=NOW)
    with LiveBettingStore(
        tmp_path / "verified.db", raw_archive_root=raw_root
    ) as store:
        store.init_schema()
        store.store_odds_observation(
            source="direct",
            observation_key="verified",
            source_event_id=None,
            raybet_match_id="1001",
            observed_at=NOW,
            normalized_state_hash=normalized_state_hash(rows),
            snapshots=rows,
            raw_payload=payload,
        )

        verified = verify_odds_response_authority(store.connection, raw_root)

        assert (
            verified.state_count,
            verified.transport_count,
            verified.artifact_count,
            verified.legacy_transport_count,
        ) == (1, 1, 1, 0)


def test_shared_authority_verifier_rejects_valid_artifact_bound_to_wrong_state(
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "raw"
    with LiveBettingStore(
        tmp_path / "binding.db", raw_archive_root=raw_root
    ) as store:
        store.init_schema()
        for index, price in enumerate(("2.10", "3.10")):
            payload = odds_payload(request_sequence=index)
            payload["result"]["odds"][0]["odds"] = price  # type: ignore[index]
            observed_at = NOW + timedelta(seconds=index)
            rows = snapshots_from_payload(payload, received_at=observed_at)
            store.store_odds_observation(
                source="direct",
                observation_key=f"binding-{index}",
                source_event_id=None,
                raybet_match_id="1001",
                observed_at=observed_at,
                normalized_state_hash=normalized_state_hash(rows),
                snapshots=rows,
                raw_payload=payload,
            )
        second_artifact = store.connection.execute(
            "SELECT response_artifact_hash FROM odds_transport_observations "
            "WHERE observation_key='binding-1'"
        ).fetchone()[0]
        store.connection.execute(
            "DROP TRIGGER odds_transport_observations_guard_update"
        )
        store.connection.execute(
            "UPDATE odds_transport_observations SET response_artifact_hash=? "
            "WHERE observation_key='binding-0'",
            (second_artifact,),
        )
        store.connection.commit()

        with pytest.raises(RuntimeError, match="state membership mismatch"):
            verify_odds_response_authority(store.connection, raw_root)


def test_unsupported_outcome_retains_empty_key_but_supported_cannot() -> None:
    unsupported = (
        "unknown",
        "winner",
        2.0,
        "5",
        "winner",
        "map_1",
        None,
        None,
        "",
        0,
        None,
    )
    assert canonical_state_outcomes([unsupported]) == (unsupported,)
    with pytest.raises(ValueError, match="supported response outcome key"):
        canonical_state_outcomes([(*unsupported[:9], 1, unsupported[10])])


def test_shared_authority_golden_identities_are_bit_exact() -> None:
    payload = odds_payload()
    snapshots = snapshots_from_payload(payload, received_at=NOW)
    normalized = normalized_state_hash(snapshots)
    state_hash, _, _ = response_state_identity(
        "1001",
        normalized,
        [
            (
                row.odds_id,
                row.odds_group_id,
                row.price,
                None if row.status is None else str(row.status),
                row.market.market_type,
                row.market.period,
                row.market.side,
                row.market.line,
                row.market.outcome_key,
                int(row.market.supported),
                row.last_update,
            )
            for row in snapshots
        ],
    )
    artifact_hash, _, _ = response_artifact_identity(
        snapshot_derived_payload("1001", [row.raw for row in snapshots])
    )
    assert legacy_normalized_state_hash_v1(snapshots) == (
        "8fe897448500647660359fc2b56a00c8011975a6e68017e9aead55b1ed244708"
    )
    assert normalized == "e5a85f74fefbabdd6268aeb067503d2286c24ebc3b5e73577f05724c7b608379"
    assert state_hash == "1879941aad46ced199e77d1de02cead3060cca8e95344ac113d0e9d5130ae560"
    assert artifact_hash == "43e441560c99d6b08f9c959f165f2c2518498f233e6f495e78902d67a1291dc2"


_BASE_OUTCOME = (
    "winner-one",
    "winner",
    2.1,
    "1",
    "winner",
    "map_1",
    "team_one",
    None,
    "team_one",
    1,
    "provider-state",
)


@pytest.mark.parametrize(
    ("index", "value"),
    (
        (0, "winner-renamed"),
        (1, "winner-renamed"),
        (2, 2.2),
        (3, "2"),
        (4, "kill_handicap"),
        (5, "map_2"),
        (6, "team_two"),
        (7, 1.5),
        (8, "team_two"),
        (9, 0),
        (10, "provider-state-2"),
    ),
)
def test_v2_normalized_hash_covers_every_semantic_field(
    index: int,
    value: object,
) -> None:
    changed = list(_BASE_OUTCOME)
    changed[index] = value
    baseline_hash, _, baseline_manifest = normalized_state_identity([_BASE_OUTCOME])
    changed_hash, _, changed_manifest = normalized_state_identity([changed])
    assert changed_manifest != baseline_manifest
    assert changed_hash != baseline_hash


def test_v2_normalized_hash_has_stable_member_order_and_strict_numbers() -> None:
    second = ("winner-two", "winner", 1.8, "1", "winner", "map_1",
              "team_two", None, "team_two", 1, "provider-state")
    assert normalized_state_identity([_BASE_OUTCOME, second]) == (
        normalized_state_identity([second, _BASE_OUTCOME])
    )
    invalid_price = list(_BASE_OUTCOME)
    invalid_price[2] = float("inf")
    with pytest.raises(ValueError, match="price must be finite"):
        normalized_state_identity([invalid_price])
    invalid_line = list(_BASE_OUTCOME)
    invalid_line[7] = True
    with pytest.raises(ValueError, match="line must be numeric"):
        normalized_state_identity([invalid_line])


@pytest.mark.parametrize(
    "field",
    (
        "odds_id",
        "odds_group_id",
        "price",
        "status",
        "market_type",
        "period",
        "side",
        "line",
        "outcome_key",
        "supported",
        "last_update",
    ),
)
def test_online_writer_rejects_each_snapshot_field_that_disagrees_with_raw(
    tmp_path: Path,
    field: str,
) -> None:
    payload = odds_payload()
    snapshots = snapshots_from_payload(payload, received_at=NOW)
    first = snapshots[0]
    market_changes: dict[str, object] = {}
    snapshot_changes: dict[str, object] = {}
    if field in {"market_type", "period", "side", "line", "outcome_key", "supported"}:
        market_changes[field] = {
            "market_type": "kill_handicap",
            "period": "map_2",
            "side": "team_two",
            "line": 1.5,
            "outcome_key": "different-outcome",
            "supported": False,
        }[field]
        snapshot_changes["market"] = replace(first.market, **market_changes)
    else:
        snapshot_changes[field] = {
            "odds_id": "different-id",
            "odds_group_id": "different-group",
            "price": 2.2,
            "status": 2,
            "last_update": "different-update",
        }[field]
    mismatched = [replace(first, **snapshot_changes), *snapshots[1:]]

    with LiveBettingStore(tmp_path / f"raw-mismatch-{field}.db") as store:
        store.init_schema()
        with pytest.raises(ValueError, match="raw semantic membership"):
            store.store_odds_observation(
                source="direct",
                observation_key=f"raw-mismatch-{field}",
                source_event_id=None,
                raybet_match_id="1001",
                observed_at=NOW,
                normalized_state_hash=normalized_state_hash(mismatched),
                snapshots=mismatched,
                raw_payload=payload,
            )
        assert scalar(store, "SELECT COUNT(*) FROM odds_response_states") == 0
        assert scalar(store, "SELECT COUNT(*) FROM odds_transport_observations") == 0


def test_online_writer_rejects_receipt_from_another_raw_response(
    tmp_path: Path,
) -> None:
    original = odds_payload(request_sequence=1)
    changed = odds_payload(request_sequence=2)
    with LiveBettingStore(tmp_path / "receipt-mismatch.db") as store:
        store.init_schema()
        receipt = store.archive_response_payload(
            original,
            observed_at=NOW,
            match_id="1001",
        )
        snapshots = snapshots_from_payload(changed, received_at=NOW)
        with pytest.raises(ValueError, match="does not match payload"):
            store.store_odds_observation(
                source="direct",
                observation_key="receipt-mismatch",
                source_event_id=None,
                raybet_match_id="1001",
                observed_at=NOW,
                normalized_state_hash=normalized_state_hash(snapshots),
                snapshots=snapshots,
                raw_payload=changed,
                raw_artifact=receipt,
            )
        assert scalar(store, "SELECT COUNT(*) FROM odds_raw_artifacts") == 0
        assert scalar(store, "SELECT COUNT(*) FROM odds_transport_observations") == 0


def test_semantic_market_change_is_persisted_when_price_fields_are_unchanged(
    tmp_path: Path,
) -> None:
    first_payload = odds_payload()
    second_payload = odds_payload()
    for item in second_payload["result"]["odds"]:  # type: ignore[index]
        item["match_stage"] = "r2"
    with LiveBettingStore(tmp_path / "semantic-change.db") as store:
        store.init_schema()
        for index, payload in enumerate((first_payload, second_payload)):
            observed_at = NOW + timedelta(seconds=index)
            snapshots = snapshots_from_payload(payload, received_at=observed_at)
            _, changes = store.store_odds_observation(
                source="direct",
                observation_key=f"semantic-{index}",
                source_event_id=None,
                raybet_match_id="1001",
                observed_at=observed_at,
                normalized_state_hash=normalized_state_hash(snapshots),
                snapshots=snapshots,
                raw_payload=payload,
            )
            assert changes == 2
        assert scalar(store, "SELECT COUNT(*) FROM odds_snapshots") == 4


def test_rejected_direct_response_is_immutable_replayable_and_not_normalized(
    tmp_path: Path,
) -> None:
    payload = odds_payload()
    payload["result"]["id"] = "wrong-match"  # type: ignore[index]
    with LiveBettingStore(tmp_path / "direct-audit.db") as store:
        store.init_schema()
        receipt = store.archive_response_payload(
            payload,
            observed_at=NOW,
            match_id="1001",
        )
        audit_key = store.record_direct_response_audit(
            receipt,
            response_kind="live_odds",
            claimed_raybet_match_id="1001",
            observed_raybet_match_id="wrong-match",
            disposition="rejected",
            reason="identity_mismatch",
        )
        assert store.direct_response_payload(audit_key) == payload
        assert scalar(store, "SELECT COUNT(*) FROM odds_raw_artifacts") == 1
        assert scalar(store, "SELECT COUNT(*) FROM direct_response_audit") == 1
        assert scalar(store, "SELECT COUNT(*) FROM odds_transport_observations") == 0
        assert scalar(store, "SELECT COUNT(*) FROM odds_response_states") == 0
        assert scalar(store, "SELECT COUNT(*) FROM odds_snapshots") == 0
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.connection.execute(
                "UPDATE direct_response_audit SET reason='forged'"
            )


def test_raw_variance_does_not_duplicate_semantic_state(tmp_path: Path) -> None:
    with LiveBettingStore(tmp_path / "direct.db") as store:
        store.init_schema()
        for index in range(100):
            observed_at = NOW + timedelta(seconds=index)
            payload = odds_payload(request_sequence=index)
            snapshots = snapshots_from_payload(payload, received_at=observed_at)
            store.store_odds_observation(
                source="direct",
                observation_key=f"direct-{index}",
                source_event_id=None,
                raybet_match_id="1001",
                observed_at=observed_at,
                normalized_state_hash=normalized_state_hash(snapshots),
                snapshots=snapshots,
                raw_payload=payload,
            )

        assert scalar(store, "SELECT COUNT(*) FROM odds_transport_observations") == 100
        assert scalar(store, "SELECT COUNT(*) FROM odds_response_states") == 1
        assert scalar(store, "SELECT COUNT(*) FROM odds_response_state_outcomes") == 2
        assert scalar(store, "SELECT COUNT(*) FROM odds_raw_artifacts") == 100
        assert scalar(store, "SELECT COUNT(*) FROM odds_response_outcomes") == 0
        assert store.response_raw_payload("direct-0")["request_sequence"] == 0
        assert store.response_raw_payload("direct-99")["request_sequence"] == 99


def test_repeated_browser_payload_is_external_and_content_addressed(
    tmp_path: Path,
) -> None:
    payload = odds_payload()
    with LiveBettingStore(tmp_path / "browser.db") as store:
        store.init_schema()
        ingestor = BrowserEventIngestor(
            clock=lambda: NOW + timedelta(seconds=200)
        )
        for index in range(100):
            assert ingestor.ingest(store, browser_event(index, payload)).outcome == "accepted"

        assert scalar(store, "SELECT COUNT(*) FROM browser_events") == 100
        assert scalar(store, "SELECT COUNT(*) FROM odds_transport_observations") == 100
        assert scalar(store, "SELECT COUNT(*) FROM odds_response_states") == 1
        assert scalar(store, "SELECT COUNT(*) FROM odds_response_state_outcomes") == 2
        assert scalar(store, "SELECT COUNT(*) FROM odds_raw_artifacts") == 1
        assert scalar(store, "SELECT COUNT(*) FROM odds_response_outcomes") == 0
        assert scalar(
            store,
            "SELECT COUNT(*) FROM browser_events "
            "WHERE payload_storage='external' AND payload_json='{}' "
            "AND payload_artifact_hash IS NOT NULL",
        ) == 100
        assert store.browser_event_payload(f"{100:064x}") == payload
        assert len(list(store.raw_archive_root.rglob("*.json.gz"))) == 1


def test_state_hash_collision_fails_closed_and_rolls_back(tmp_path: Path) -> None:
    with LiveBettingStore(tmp_path / "state-collision.db") as store:
        store.init_schema()
        with patch(
            "live_betting.odds_response_authority._content_hash",
            return_value="f" * 64,
        ):
            first = odds_payload()
            first_rows = snapshots_from_payload(first, received_at=NOW)
            store.store_odds_observation(
                source="direct",
                observation_key="first",
                source_event_id=None,
                raybet_match_id="1001",
                observed_at=NOW,
                normalized_state_hash=normalized_state_hash(first_rows),
                snapshots=first_rows,
                raw_payload=first,
            )
            changed = odds_payload()
            changed["result"]["odds"][0]["odds"] = "3.25"  # type: ignore[index]
            changed_rows = snapshots_from_payload(
                changed, received_at=NOW + timedelta(seconds=1)
            )
            with pytest.raises(ValueError, match="state hash collision"):
                store.store_odds_observation(
                    source="direct",
                    observation_key="collision",
                    source_event_id=None,
                    raybet_match_id="1001",
                    observed_at=NOW + timedelta(seconds=1),
                    normalized_state_hash=normalized_state_hash(changed_rows),
                    snapshots=changed_rows,
                    raw_payload=changed,
                )

        assert scalar(store, "SELECT COUNT(*) FROM odds_transport_observations") == 1
        assert scalar(store, "SELECT COUNT(*) FROM odds_response_states") == 1
        assert scalar(store, "SELECT COUNT(*) FROM odds_raw_artifacts") == 1


def test_normalized_v2_hash_collision_with_different_manifest_fails_closed(
    tmp_path: Path,
) -> None:
    def forced_collision(outcomes):
        ordered = canonical_state_outcomes(outcomes)
        return "e" * 64, ordered, b"forced-normalized-collision"

    with LiveBettingStore(tmp_path / "normalized-collision.db") as store:
        store.init_schema()
        with (
            patch(
                "live_betting.markets.normalized_state_identity",
                side_effect=forced_collision,
            ),
            patch(
                "live_betting.odds_response_authority.normalized_state_identity",
                side_effect=forced_collision,
            ),
        ):
            first = odds_payload()
            first_rows = snapshots_from_payload(first, received_at=NOW)
            store.store_odds_observation(
                source="direct",
                observation_key="normalized-first",
                source_event_id=None,
                raybet_match_id="1001",
                observed_at=NOW,
                normalized_state_hash=normalized_state_hash(first_rows),
                snapshots=first_rows,
                raw_payload=first,
            )
            changed = odds_payload()
            changed["result"]["odds"][0]["match_stage"] = "r2"  # type: ignore[index]
            changed_rows = snapshots_from_payload(
                changed,
                received_at=NOW + timedelta(seconds=1),
            )
            with pytest.raises(ValueError, match="different response manifest"):
                store.store_odds_observation(
                    source="direct",
                    observation_key="normalized-second",
                    source_event_id=None,
                    raybet_match_id="1001",
                    observed_at=NOW + timedelta(seconds=1),
                    normalized_state_hash=normalized_state_hash(changed_rows),
                    snapshots=changed_rows,
                    raw_payload=changed,
                )

        assert scalar(store, "SELECT COUNT(*) FROM odds_transport_observations") == 1
        assert scalar(store, "SELECT COUNT(*) FROM odds_response_states") == 1


def test_raw_artifact_hash_collision_fails_closed(tmp_path: Path) -> None:
    with LiveBettingStore(tmp_path / "artifact-collision.db") as store:
        store.init_schema()
        first = store.archive_response_payload(
            odds_payload(request_sequence=1), observed_at=NOW, match_id="1001"
        )
        second = store.archive_response_payload(
            odds_payload(request_sequence=2),
            observed_at=NOW + timedelta(seconds=1),
            match_id="1001",
        )
        store._register_raw_artifact(first)
        forged = replace(second, content_sha256=first.content_sha256)
        with pytest.raises(RuntimeError, match="hash mismatch"):
            store._register_raw_artifact(forged)
        assert scalar(store, "SELECT COUNT(*) FROM odds_raw_artifacts") == 1


def test_normalization_fault_rolls_back_all_database_references(
    tmp_path: Path,
) -> None:
    with LiveBettingStore(tmp_path / "fault.db") as store:
        store.init_schema()
        payload = odds_payload()
        rows = snapshots_from_payload(payload, received_at=NOW)
        original = store.insert_odds
        calls = 0

        def fail_second(snapshot):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected normalization fault")
            return original(snapshot)

        with patch.object(store, "insert_odds", side_effect=fail_second):
            with pytest.raises(RuntimeError, match="injected normalization fault"):
                store.store_odds_observation(
                    source="direct",
                    observation_key="fault",
                    source_event_id=None,
                    raybet_match_id="1001",
                    observed_at=NOW,
                    normalized_state_hash=normalized_state_hash(rows),
                    snapshots=rows,
                    raw_payload=payload,
                )

        for relation in (
            "odds_raw_artifacts",
            "odds_response_states",
            "odds_response_state_outcomes",
            "odds_transport_observations",
            "odds_response_outcomes",
            "odds_snapshots",
        ):
            assert scalar(store, f"SELECT COUNT(*) FROM {relation}") == 0
        assert len(list(store.raw_archive_root.rglob("*.json.gz"))) == 1


def test_v2_content_and_transport_refs_are_immutable_and_legacy_writes_stop(
    tmp_path: Path,
) -> None:
    with LiveBettingStore(tmp_path / "immutable.db") as store:
        store.init_schema()
        payload = odds_payload()
        rows = snapshots_from_payload(payload, received_at=NOW)
        store.store_odds_observation(
            source="direct",
            observation_key="immutable",
            source_event_id=None,
            raybet_match_id="1001",
            observed_at=NOW,
            normalized_state_hash=normalized_state_hash(rows),
            snapshots=rows,
            raw_payload=payload,
        )

        for statement in (
            "UPDATE odds_raw_artifacts SET uncompressed_bytes=99",
            "DELETE FROM odds_raw_artifacts",
            "UPDATE odds_response_states SET outcome_count=99",
            "DELETE FROM odds_response_states",
            "UPDATE odds_response_state_outcomes SET price=9.0",
            "DELETE FROM odds_response_state_outcomes",
            "UPDATE odds_transport_observations SET response_state_hash='" + "a" * 64 + "'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                store.connection.execute(statement)

        with pytest.raises(sqlite3.IntegrityError, match="legacy.*disabled"):
            store.connection.execute(
                """INSERT INTO odds_response_outcomes
                   (observation_key, raybet_match_id, odds_id, odds_group_id,
                    received_at, price, status, market_type, period, side, line,
                    outcome_key, supported, last_update, raw_json)
                   VALUES ('immutable', '1001', 'forged', NULL, ?, 2.0, '1',
                           'winner', 'map_1', 'team_one', NULL, 'team_one', 1,
                           NULL, '{}')""",
                (NOW.isoformat(),),
            )


def test_raybet_archive_hot_path_never_scans_existing_tree(tmp_path: Path) -> None:
    legacy = tmp_path / "raybet" / "legacy"
    legacy.mkdir(parents=True)
    for index in range(1_000):
        (legacy / f"legacy-{index}.json.gz").touch()
    payload = odds_payload()
    with patch.object(Path, "rglob", side_effect=AssertionError("tree scan")):
        first = RawArchive(tmp_path).archive_json(
            source="raybet",
            endpoint="https://raybet.local/v2/odds",
            request_identity="https://raybet.local/v2/odds?match_id=1001",
            payload_bytes=canonical_json(payload),
            observed_at=NOW,
            match_id=1001,
            status_code=None,
        )
        second = RawArchive(tmp_path).archive_json(
            source="raybet",
            endpoint="https://raybet.local/v2/odds",
            request_identity="https://raybet.local/v2/odds?match_id=1001",
            payload_bytes=canonical_json(payload),
            observed_at=NOW + timedelta(seconds=1),
            match_id=1001,
            status_code=None,
        )
    assert first.path == second.path
    assert first.artifact_created is True
    assert second.artifact_created is False


def test_database_and_raw_tree_can_relocate_and_missing_artifact_fails_closed(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    original.mkdir()
    database = original / "browser.db"
    payload = odds_payload()
    with LiveBettingStore(database) as store:
        store.init_schema()
        result = BrowserEventIngestor(
            clock=lambda: NOW + timedelta(seconds=2)
        ).ingest(store, browser_event(0, payload))
        assert result.outcome == "accepted"
        relative_path = Path(
            str(
                store.connection.execute(
                    "SELECT storage_path FROM odds_raw_artifacts"
                ).fetchone()[0]
            )
        )
        assert not relative_path.is_absolute()
        assert ".." not in relative_path.parts

    relocated = tmp_path / "relocated"
    relocated.mkdir()
    relocated_database = relocated / "browser.db"
    shutil.copy2(database, relocated_database)
    shutil.copytree(
        original / "live_betting" / "raw-v2",
        relocated / "live_betting" / "raw-v2",
    )
    with LiveBettingStore(relocated_database) as store:
        assert store.browser_event_payload(f"{1:064x}") == payload
        assert store.response_raw_payload(f"{1:064x}") == payload
        artifact = next(store.raw_archive_root.rglob("*.json.gz"))
        artifact.unlink()
        with pytest.raises(RuntimeError, match="artifact"):
            store.browser_event_payload(f"{1:064x}")

    shutil.copy2(
        next((original / "live_betting" / "raw-v2").rglob("*.json.gz")),
        relocated / "live_betting" / "raw-v2" / relative_path,
    )
    with LiveBettingStore(relocated_database) as store:
        artifact = next(store.raw_archive_root.rglob("*.json.gz"))
        artifact.write_bytes(b"not-gzip")
        with pytest.raises(RuntimeError, match="artifact"):
            store.response_raw_payload(f"{1:064x}")
