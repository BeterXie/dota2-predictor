from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from live_betting.rosh_parity_storage import (
    RoshEvidenceCollisionError,
    RoshHeroScoreRecord,
    RoshMinutePointRecord,
    RoshRunRecord,
    RoshRunRepository,
)
from live_betting.storage import LiveBettingStore


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _draft() -> dict[str, list[dict[str, int]]]:
    return {
        "radiant": [
            {"hero_id": position, "position_id": position}
            for position in range(1, 6)
        ],
        "dire": [
            {"hero_id": position + 5, "position_id": position}
            for position in range(1, 6)
        ],
    }


def _run(*, status: str = "succeeded") -> RoshRunRecord:
    succeeded = status == "succeeded"
    return RoshRunRecord(
        run_id=_hash(f"run-{status}"),
        status=status,
        mode="historical_match",
        match_id=8904419709,
        date_time=1784485548,
        draft_hash=_hash("draft"),
        draft=_draft(),
        rosh_profile_id="stratz-rosh-web-2026-07-28-v2",
        formula_version="stratz-official-rosh/2026-07-28-v2",
        request_profile_hash=_hash("request-profile"),
        upstream_bundle_hash=_hash("upstream-bundle"),
        scorer_source_hash=_hash("scorer-source"),
        canonical_profile_hash=_hash("canonical-profile"),
        serialization_version="rfc8785-jcs/v1",
        request_hash=_hash("request"),
        request_manifest={
            "schema": "rosh-request-manifest/v1",
            "operations": ["GetMatchPicksBans"],
        },
        response_manifest=(
            {
                "operation_name": "GetMatchPicksBans",
                "request_artifact_hash": _hash("request-artifact"),
                "response_artifact_hash": _hash("response-artifact"),
                "collected_at": "2026-07-29T00:00:00Z",
                "relative_path": "stratz/GetMatchPicksBans.json",
            },
        )
        if succeeded
        else (),
        evidence_hash=_hash(f"evidence-{status}"),
        collected_at="2026-07-29T00:00:01Z",
        radiant_team_score=-4.9 if succeeded else None,
        dire_team_score=-10.7 if succeeded else None,
        relative_advantage=5.8 if succeeded else None,
        error_code=None if succeeded else "profile_drift",
    )


def _heroes() -> tuple[RoshHeroScoreRecord, ...]:
    return tuple(
        RoshHeroScoreRecord(
            team_side=side,
            position_id=position,
            hero_id=position + offset,
            raw_score=float(position) / 10,
            display_score=float(position),
            components={
                "position_base_diff": 0.1,
                "same_team_synergy": 0.2,
                "opponent_matchup_synergy": -0.3,
            },
        )
        for side, offset in (("RADIANT", 0), ("DIRE", 5))
        for position in range(1, 6)
    )


def _minutes() -> tuple[RoshMinutePointRecord, ...]:
    return tuple(
        RoshMinutePointRecord(
            minute=minute,
            raw_score=minute / 10,
            display_score=float(minute),
            radiant_time_delta=0.1,
            dire_time_delta=-0.1,
            synergy_delta=0.25,
            source_audit={
                "rank_source_counts": {
                    "DIVINE_IMMORTAL": 6,
                    "ALL_RANK_FALLBACK": 4,
                },
                "slots": [],
            },
        )
        for minute in range(20, 61)
    )


@pytest.fixture
def repository() -> tuple[LiveBettingStore, RoshRunRepository]:
    store = LiveBettingStore(":memory:")
    store.init_schema()
    yield store, RoshRunRepository(store.connection)
    store.close()


def test_succeeded_run_writes_ten_heroes_and_41_minutes_atomically_and_roundtrips(
    repository: tuple[LiveBettingStore, RoshRunRepository],
) -> None:
    store, repo = repository

    stored = repo.write_succeeded(_run(), _heroes(), _minutes())

    assert stored.run == _run()
    assert len(stored.hero_scores) == 10
    assert len(stored.minute_points) == 41
    assert stored.result is not None
    assert stored.result["relative_advantage"] == 5.8
    assert [hero.team_side for hero in stored.hero_scores[:5]] == ["RADIANT"] * 5
    assert store.connection.execute(
        "SELECT COUNT(*) FROM rosh_hero_scores"
    ).fetchone()[0] == 10
    assert store.connection.execute(
        "SELECT COUNT(*) FROM rosh_minute_points"
    ).fetchone()[0] == 41
    minute_rows = {
        int(row[0]): row
        for row in store.connection.execute(
            """SELECT minute, raw_score, display_score, radiant_time_delta,
                      dire_time_delta, synergy_delta, source_audit_json
                 FROM rosh_minute_points WHERE run_id=?""",
            (stored.run.run_id,),
        )
    }
    assert stored.result is not None
    for point in stored.result["minute_points"]:
        row = minute_rows[point["minute"]]
        assert point["raw_score"] == row[1]
        assert point["display_score"] == row[2]
        assert point["radiant_time_delta"] == row[3]
        assert point["dire_time_delta"] == row[4]
        assert point["synergy_delta"] == row[5]
        for key, value in json.loads(row[6]).items():
            assert point[key] == value


def test_latest_succeeded_draft_lookup_is_profile_bound_and_causal(
    repository: tuple[LiveBettingStore, RoshRunRepository],
) -> None:
    _store, repo = repository
    stored = repo.write_succeeded(_run(), _heroes(), _minutes())

    assert repo.get_latest_succeeded_for_draft(
        stored.run.draft_hash,
        rosh_profile_id=stored.run.rosh_profile_id,
        collected_at_lte=datetime(2026, 7, 28, tzinfo=timezone.utc),
    ) is None
    assert repo.get_latest_succeeded_for_draft(
        stored.run.draft_hash,
        rosh_profile_id="different-profile",
        collected_at_lte=datetime(2026, 7, 30, tzinfo=timezone.utc),
    ) is None
    assert repo.get_latest_succeeded_for_draft(
        stored.run.draft_hash,
        rosh_profile_id=stored.run.rosh_profile_id,
        collected_at_lte=datetime(2026, 7, 30, tzinfo=timezone.utc),
    ) == stored


def test_failed_run_is_terminal_and_has_no_result_or_children(
    repository: tuple[LiveBettingStore, RoshRunRepository],
) -> None:
    store, repo = repository

    stored = repo.write_failed(_run(status="failed"))

    assert stored.run.error_code == "profile_drift"
    assert stored.result is None
    assert stored.hero_scores == ()
    assert stored.minute_points == ()
    with pytest.raises(sqlite3.IntegrityError, match="requires succeeded run"):
        store.connection.execute(
            """INSERT INTO rosh_minute_points
               VALUES (?, 0, 0, 0, 0, 0, 0, '{}')""",
            (stored.run.run_id,),
        )


def test_fault_after_partial_children_rolls_back_the_whole_run(
    repository: tuple[LiveBettingStore, RoshRunRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repo = repository

    def fail(stage: str, index: int | None = None) -> None:
        if stage == "minute" and index == 3:
            raise RuntimeError("injected repository fault")

    monkeypatch.setattr(repo, "_checkpoint", fail)

    with pytest.raises(RuntimeError, match="injected repository fault"):
        repo.write_succeeded(_run(), _heroes(), _minutes())
    for table in ("rosh_analysis_runs", "rosh_hero_scores", "rosh_minute_points"):
        assert store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_repository_respects_an_existing_external_transaction(
    repository: tuple[LiveBettingStore, RoshRunRepository],
) -> None:
    store, repo = repository
    store.connection.execute("BEGIN")

    repo.write_failed(_run(status="failed"))

    assert store.connection.in_transaction
    store.connection.rollback()
    assert repo.get(_run(status="failed").run_id) is None


def test_identical_evidence_is_idempotent_but_contradictory_content_fails_closed(
    repository: tuple[LiveBettingStore, RoshRunRepository],
) -> None:
    store, repo = repository
    first = repo.write_succeeded(_run(), _heroes(), _minutes())

    second = repo.write_succeeded(_run(), _heroes(), _minutes())

    assert second == first
    assert store.connection.execute(
        "SELECT COUNT(*) FROM rosh_analysis_runs"
    ).fetchone()[0] == 1
    contradictory = replace(_run(), relative_advantage=99.0)
    with pytest.raises(RoshEvidenceCollisionError, match="conflicts"):
        repo.write_succeeded(contradictory, _heroes(), _minutes())


@pytest.mark.parametrize(
    ("table", "column"),
    (
        ("rosh_analysis_runs", "formula_version"),
        ("rosh_hero_scores", "display_score"),
        ("rosh_minute_points", "display_score"),
    ),
)
def test_all_rosh_tables_reject_update_and_delete(
    repository: tuple[LiveBettingStore, RoshRunRepository],
    table: str,
    column: str,
) -> None:
    store, repo = repository
    repo.write_succeeded(_run(), _heroes(), _minutes())

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.connection.execute(f"UPDATE {table} SET {column}={column}")
    store.connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.connection.execute(f"DELETE FROM {table}")
    store.connection.rollback()


@pytest.mark.parametrize(
    ("invalid_run", "message"),
    (
        (replace(_run(), run_id="A" * 64), "lowercase hexadecimal"),
        (replace(_run(), scorer_source_hash="0" * 64), "placeholder hash"),
        (replace(_run(), formula_version="placeholder"), "non-placeholder"),
        (replace(_run(), status="running"), "status"),
        (replace(_run(), relative_advantage=float("nan")), "finite number"),
        (
            replace(_run(), request_manifest={"bad": float("inf")}),
            "finite number",
        ),
        (
            replace(_run(), request_manifest={"Authorization": "secret"}),
            "secret field",
        ),
        (replace(_run(), request_manifest={}), "non-empty request_manifest"),
    ),
)
def test_invalid_run_identity_status_and_finite_json_are_rejected_in_memory(
    repository: tuple[LiveBettingStore, RoshRunRepository],
    invalid_run: RoshRunRecord,
    message: str,
) -> None:
    _, repo = repository

    with pytest.raises(ValueError, match=message):
        repo.write_succeeded(invalid_run, _heroes(), _minutes())


@pytest.mark.parametrize("mutation", ("side", "slot", "duplicate_hero"))
def test_invalid_hero_side_slot_and_global_identity_are_rejected(
    repository: tuple[LiveBettingStore, RoshRunRepository],
    mutation: str,
) -> None:
    _, repo = repository
    heroes = list(_heroes())
    if mutation == "side":
        heroes[0] = replace(heroes[0], team_side="radiant")
    elif mutation == "slot":
        heroes[0] = replace(heroes[0], position_id=6)
    else:
        heroes[-1] = replace(heroes[-1], hero_id=heroes[0].hero_id)

    with pytest.raises(ValueError):
        repo.write_succeeded(_run(), heroes, _minutes())


@pytest.mark.parametrize(
    "reserved_key",
    (
        "minute",
        "raw_score",
        "display_score",
        "radiant_time_delta",
        "dire_time_delta",
        "synergy_delta",
    ),
)
def test_minute_source_audit_cannot_override_projection_core_fields(
    repository: tuple[LiveBettingStore, RoshRunRepository],
    reserved_key: str,
) -> None:
    store, repo = repository
    minutes = list(_minutes())
    minutes[0] = replace(
        minutes[0],
        source_audit={**minutes[0].source_audit, reserved_key: "attacker-value"},
    )

    with pytest.raises(ValueError, match="reserved core field"):
        repo.write_succeeded(_run(), _heroes(), minutes)
    for table in ("rosh_analysis_runs", "rosh_hero_scores", "rosh_minute_points"):
        assert store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("target", "secret_key"),
    (
        ("request", "Authorization"),
        ("response", "Cookie"),
        ("response", "X-Api-Key"),
        ("response", "Proxy-Authorization"),
        ("response", "Set-Cookie"),
        ("hero", "accessToken"),
        ("hero", "authToken"),
        ("hero", "X-Auth-Token"),
        ("minute", "browserSession"),
        ("minute", "sessionCookie"),
        ("request", "refreshToken"),
        ("response", "apiToken"),
        ("hero", "idToken"),
        ("minute", "bearerToken"),
        ("request", "cookieHeader"),
        ("response", "authorizationHeader"),
        ("hero", "clientSecret"),
        ("minute", "headers"),
        ("request", "requestHeaders"),
        ("response", "responseHeaders"),
    ),
)
def test_open_json_rejects_secret_key_variants_before_any_write(
    repository: tuple[LiveBettingStore, RoshRunRepository],
    target: str,
    secret_key: str,
) -> None:
    store, repo = repository
    run = _run()
    heroes = list(_heroes())
    minutes = list(_minutes())
    if target == "request":
        run = replace(
            run,
            request_manifest={**run.request_manifest, secret_key: "secret-value"},
        )
    elif target == "response":
        response = dict(run.response_manifest[0])
        response[secret_key] = "secret-value"
        run = replace(run, response_manifest=(response,))
    elif target == "hero":
        heroes[0] = replace(
            heroes[0], components={**heroes[0].components, secret_key: "secret-value"}
        )
    else:
        minutes[0] = replace(
            minutes[0],
            source_audit={**minutes[0].source_audit, secret_key: "secret-value"},
        )

    with pytest.raises(ValueError, match="forbidden secret field") as raised:
        repo.write_succeeded(run, heroes, minutes)
    assert "secret-value" not in str(raised.value)
    assert secret_key not in str(raised.value)
    for table in ("rosh_analysis_runs", "rosh_hero_scores", "rosh_minute_points"):
        assert store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_open_json_allows_noncredential_token_count_diagnostics(
    repository: tuple[LiveBettingStore, RoshRunRepository],
) -> None:
    _, repo = repository
    run = replace(
        _run(),
        request_manifest={**_run().request_manifest, "token_count": 3},
        response_manifest=(
            {**_run().response_manifest[0], "tokenCount": 4},
        ),
    )
    heroes = list(_heroes())
    heroes[0] = replace(
        heroes[0], components={**heroes[0].components, "token_count": 5}
    )
    minutes = list(_minutes())
    minutes[0] = replace(
        minutes[0],
        source_audit={**minutes[0].source_audit, "tokenCount": 6},
    )

    stored = repo.write_succeeded(run, heroes, minutes)

    assert stored.result is not None
    assert stored.result["hero_scores"][0]["token_count"] == 5
    assert stored.result["minute_points"][0]["tokenCount"] == 6


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("rosh_profile_id", "placeholder-v2"),
        ("formula_version", "official-pending-v2"),
        ("serialization_version", "candidate/v1"),
        ("serialization_version", "officialCandidateV2"),
        ("rosh_profile_id", "official-unactivated-v2"),
        ("formula_version", "official-v2-superseded"),
        ("serialization_version", " rfc8785-jcs/v1"),
        ("rosh_profile_id", "stratz-rosh-v2 "),
    ),
)
def test_profile_identity_rejects_governance_placeholders_and_outer_whitespace(
    repository: tuple[LiveBettingStore, RoshRunRepository],
    field: str,
    value: str,
) -> None:
    store, repo = repository

    with pytest.raises(ValueError, match="canonical non-placeholder identity"):
        repo.write_succeeded(replace(_run(), **{field: value}), _heroes(), _minutes())
    assert store.connection.execute(
        "SELECT COUNT(*) FROM rosh_analysis_runs"
    ).fetchone()[0] == 0


def test_negative_zero_is_canonical_and_repeated_write_is_idempotent(
    repository: tuple[LiveBettingStore, RoshRunRepository],
) -> None:
    store, repo = repository
    run = replace(
        _run(),
        radiant_team_score=-0.0,
        dire_team_score=-0.0,
        relative_advantage=-0.0,
    )
    heroes = list(_heroes())
    heroes[0] = replace(
        heroes[0],
        raw_score=-0.0,
        display_score=-0.0,
        components={name: -0.0 for name in heroes[0].components},
    )
    minutes = list(_minutes())
    minutes[0] = replace(
        minutes[0],
        raw_score=-0.0,
        display_score=-0.0,
        radiant_time_delta=-0.0,
        dire_time_delta=-0.0,
        synergy_delta=-0.0,
        source_audit={
            **minutes[0].source_audit,
            "nested_zero": {"value": -0.0},
        },
    )

    first = repo.write_succeeded(run, heroes, minutes)
    second = repo.write_succeeded(run, heroes, minutes)

    assert second == first
    assert store.connection.execute(
        "SELECT COUNT(*) FROM rosh_analysis_runs"
    ).fetchone()[0] == 1
    persisted_json = store.connection.execute(
        """SELECT result_json FROM rosh_analysis_runs WHERE run_id=?
           UNION ALL
           SELECT components_json FROM rosh_hero_scores
            WHERE run_id=? AND team_side='RADIANT' AND position_id=1
           UNION ALL
           SELECT source_audit_json FROM rosh_minute_points
            WHERE run_id=? AND minute=20""",
        (run.run_id, run.run_id, run.run_id),
    ).fetchall()
    result = json.loads(persisted_json[0][0])
    assert [
        result["radiant_team_score"],
        result["dire_team_score"],
        result["relative_advantage"],
    ] == [0, 0, 0]
    assert all(
        result["hero_scores"][0][field] == 0
        for field in (
            "raw_score",
            "display_score",
            "position_base_diff",
            "same_team_synergy",
            "opponent_matchup_synergy",
        )
    )
    assert all(
        result["minute_points"][0][field] == 0
        for field in (
            "raw_score",
            "display_score",
            "radiant_time_delta",
            "dire_time_delta",
            "synergy_delta",
        )
    )
    assert result["minute_points"][0]["nested_zero"]["value"] == 0
    assert set(json.loads(persisted_json[1][0]).values()) == {0}
    assert json.loads(persisted_json[2][0])["nested_zero"]["value"] == 0


@pytest.mark.parametrize(
    "relative_path",
    (
        "/secrets/raw.json",
        "C:/secrets/raw.json",
        "C:\\secrets\\raw.json",
        "\\secrets\\raw.json",
        "\\\\server\\share\\raw.json",
        "../secrets/raw.json",
        "",
        ".",
        "./",
        ".\\",
        "file:///C:/secret.json",
    ),
)
def test_response_manifest_rejects_non_relative_artifact_paths(
    repository: tuple[LiveBettingStore, RoshRunRepository],
    relative_path: str,
) -> None:
    store, repo = repository
    response = dict(_run().response_manifest[0])
    response["relative_path"] = relative_path

    with pytest.raises(ValueError, match="relative_path"):
        repo.write_succeeded(
            replace(_run(), response_manifest=(response,)), _heroes(), _minutes()
        )
    assert store.connection.execute(
        "SELECT COUNT(*) FROM rosh_analysis_runs"
    ).fetchone()[0] == 0


def test_getters_are_select_only_and_never_refresh_or_update(
    repository: tuple[LiveBettingStore, RoshRunRepository],
) -> None:
    store, repo = repository
    stored = repo.write_succeeded(_run(), _heroes(), _minutes())
    statements: list[str] = []
    store.connection.set_trace_callback(statements.append)

    assert repo.get(stored.run.run_id) == stored
    assert repo.get_by_evidence_hash(stored.run.evidence_hash) == stored

    store.connection.set_trace_callback(None)
    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
