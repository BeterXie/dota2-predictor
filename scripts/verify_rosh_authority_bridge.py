"""Validate 20/100 strict R.O.S.H. bridge writes in an isolated PostgreSQL DB."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import build_engine, require_database_url  # noqa: E402
from database.session import PostgresSession  # noqa: E402
from event_intelligence.draft_features import (  # noqa: E402
    AvailabilityMode,
    DerivedFactProvenance,
    DraftPlayer,
    DraftTarget,
    DraftTeam,
    ExpectedRoleAssignment,
)
from event_intelligence.models import RolePurpose  # noqa: E402
from event_intelligence.raw_archive import canonical_json_bytes  # noqa: E402
from event_intelligence.roles import RoleSource  # noqa: E402
from event_intelligence.rosh_authority_bridge import (  # noqa: E402
    ROSH_BRIDGE_LINEAGE_SCHEMA,
    audit_rosh_authority_bridge,
    persist_rosh_authority_bridge,
    replay_rosh_authority_bridge_record,
)
from live_betting.rosh_evidence import official_rosh_draft_hash  # noqa: E402
from live_betting.rosh_parity import ExactByteArtifactStore  # noqa: E402
from live_betting.rosh_parity_storage import (  # noqa: E402
    RoshHeroScoreRecord,
    RoshMinutePointRecord,
    RoshRunRecord,
    RoshRunRepository,
)
from prematch.stratz_official_profile import (  # noqa: E402
    build_official_request_plan,
    get_profile,
)
from prematch.stratz_official_score import (  # noqa: E402
    normalize_official_responses,
    score_official_rosh,
)


UTC = timezone.utc
MATCH_ID = 8_904_419_709
DATE_TIME = 1_784_485_548
MATCH_AT = datetime.fromtimestamp(DATE_TIME, UTC)
GENERATED_AT = MATCH_AT + timedelta(hours=1)
AVAILABLE_AT = MATCH_AT + timedelta(hours=2)
CUTOFF = MATCH_AT + timedelta(hours=4)
RADIANT = (54, 120, 28, 90, 123)
DIRE = (145, 74, 96, 79, 87)
RADIANT_PLAYERS = (101, 102, 103, 104, 105)
DIRE_PLAYERS = (201, 202, 203, 204, 205)
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "stratz_official_rosh"
    / str(MATCH_ID)
    / "responses.sanitized.json"
)


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _legacy_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _plan_body(plan: Any, index: int) -> bytes:
    payload = [
        {
            "operationName": operation.operation_name,
            "variables": _json_value(operation.variables),
            "query": operation.query,
        }
        for operation in plan.operations
    ]
    return (" " * index + json.dumps(payload, separators=(",", ":"))).encode()


def _team(
    team_id: int,
    heroes: Sequence[int],
    players: Sequence[int],
) -> DraftTeam:
    return DraftTeam(
        team_id,
        tuple(
            DraftPlayer(
                player_id=player_id,
                hero_id=hero_id,
                expected_role=ExpectedRoleAssignment(
                    purpose=RolePurpose.EXPECTED_POSITION,
                    source=RoleSource.HISTORICAL_PATTERN,
                    position=position,
                    confidence=1.0,
                    provenance=DerivedFactProvenance(
                        cutoff=GENERATED_AT,
                        first_usable_at=GENERATED_AT,
                        input_hash=_hash(
                            {
                                "team_id": team_id,
                                "position": position,
                                "player_id": player_id,
                            }
                        ),
                        version="rosh-bridge-verification-role-v1",
                    ),
                ),
            )
            for position, (hero_id, player_id) in enumerate(
                zip(heroes, players, strict=True), 1
            )
        ),
    )


def _target() -> DraftTarget:
    return DraftTarget(
        match_id=MATCH_ID,
        prediction_cutoff=CUTOFF,
        event_id="rosh-bridge-verification",
        patch=741,
        radiant=_team(10, RADIANT, RADIANT_PLAYERS),
        dire=_team(20, DIRE, DIRE_PLAYERS),
        availability_mode=AvailabilityMode.RECONSTRUCTED,
        series_id=1,
        map_number=1,
    )


def _seed_match_authority(session: PostgresSession) -> None:
    session.execute(
        """INSERT INTO event_registry
           (event_id, canonical_name, tier, prize_pool_usd,
            main_event_start_at, main_event_end_at, opendota_league_id,
            official_evidence_urls_json, evidence_status,
            scope_policy_version, scope, approval_status, approved_by,
            approved_at, reconciliation_status, included_stages_json,
            excluded_categories_json, created_at, updated_at)
           VALUES (?, ?, 'tier_1', 1000000, ?, ?, 99030, '[]',
                   'manually_audited', 'strict-t1-t2-main-event-v2',
                   'formal_main_event', 'approved', 'verification', ?,
                   'not_required', '[]', '[]', ?, ?)""",
        (
            "rosh-bridge-verification",
            "R.O.S.H. Bridge Verification",
            (CUTOFF - timedelta(days=1)).isoformat(),
            (CUTOFF + timedelta(days=1)).isoformat(),
            GENERATED_AT.isoformat(),
            GENERATED_AT.isoformat(),
            GENERATED_AT.isoformat(),
        ),
    )
    session.execute(
        """INSERT INTO matches
           (match_id, radiant_team_id, dire_team_id, radiant_win,
            duration, start_time, series_id, patch)
           VALUES (?, 10, 20, TRUE, 2207, ?, 1, 741)""",
        (MATCH_ID, DATE_TIME),
    )
    session.execute(
        """INSERT INTO match_ingest_status
           (match_id, event_id, start_time, series_id, map_number,
            stage_scope, stage_in_scope, has_valid_result,
            is_exhibition, is_forfeit, is_void_remake, draft_readiness,
            discovered_at, updated_at)
           VALUES (?, 'rosh-bridge-verification', ?, 1, 1,
                   'main_event', 1, 1, 0, 0, 0, 'ready', ?, ?)""",
        (MATCH_ID, DATE_TIME, GENERATED_AT.isoformat(), GENERATED_AT.isoformat()),
    )


def _result_records(result: Any) -> tuple[tuple[RoshHeroScoreRecord, ...], tuple[RoshMinutePointRecord, ...]]:
    heroes = tuple(
        RoshHeroScoreRecord(
            team_side=row.team_side,
            position_id=row.position_id,
            hero_id=row.hero_id,
            raw_score=row.raw_score,
            display_score=row.display_score,
            components={
                "position_base_diff": row.position_base_diff,
                "same_team_synergy": row.same_team_synergy,
                "opponent_matchup_synergy": row.opponent_matchup_synergy,
            },
        )
        for row in result.hero_scores
    )
    minutes = tuple(
        RoshMinutePointRecord(
            minute=row.minute,
            raw_score=row.raw_score,
            display_score=row.display_score,
            radiant_time_delta=row.radiant_time_delta,
            dire_time_delta=row.dire_time_delta,
            synergy_delta=row.synergy_delta,
            source_audit={
                "rank_source_counts": dict(row.rank_source_counts),
                "slots": [slot.projection() for slot in row.slots],
            },
        )
        for row in result.minute_points
    )
    return heroes, minutes


def _seed_runs_and_legacy(
    session: PostgresSession,
    artifact_root: Path,
    count: int,
) -> None:
    profile = get_profile()
    analysis_input = {
        "mode": "historical_match",
        "match_id": MATCH_ID,
        "date_time": DATE_TIME,
        "bracket_ids": ["IMMORTAL"],
    }
    plan = build_official_request_plan(
        analysis_input,
        profile=profile,
        request_started_at=GENERATED_AT,
    )
    responses = tuple(json.loads(FIXTURE.read_bytes()))
    normalized = normalize_official_responses(plan, responses)
    result = score_official_rosh(normalized, profile)
    heroes, minutes = _result_records(result)
    artifacts = ExactByteArtifactStore(artifact_root)
    response_body = FIXTURE.read_bytes()
    response_receipt = artifacts.persist(response_body)
    collected_at = AVAILABLE_AT.isoformat().replace("+00:00", "Z")
    draft = {
        side.lower(): [
            {"hero_id": slot.hero_id, "position_id": slot.position_id}
            for slot in normalized.draft
            if slot.team_side == side
        ]
        for side in ("RADIANT", "DIRE")
    }
    draft_hash = official_rosh_draft_hash(RADIANT, DIRE)
    repository = RoshRunRepository(session)
    for index in range(count):
        request_body = _plan_body(plan, index)
        request_receipt = artifacts.persist(request_body)
        request_artifact = {
            "content_sha256": request_receipt.content_sha256,
            "gzip_sha256": request_receipt.gzip_sha256,
            "relative_path": request_receipt.relative_path,
            "byte_count": request_receipt.byte_count,
        }
        request_manifest = {
            "schema": "rosh-request-manifest/v1",
            "request_hash": plan.request_hash,
            "request_body_sha256": hashlib.sha256(request_body).hexdigest(),
            "operations": [
                {
                    "index": operation.index,
                    "operation_name": operation.operation_name,
                    "query_sha256": operation.query_sha256,
                    "variables": _json_value(operation.variables),
                }
                for operation in plan.operations
            ],
            "request_artifact": request_artifact,
        }
        response_manifest = tuple(
            {
                "operation_name": operation.operation_name,
                "operation_index": operation.index,
                "request_artifact_hash": request_receipt.content_sha256,
                "response_artifact_hash": response_receipt.content_sha256,
                "collected_at": collected_at,
                "relative_path": response_receipt.relative_path,
                "request_relative_path": request_receipt.relative_path,
                "response_gzip_sha256": response_receipt.gzip_sha256,
            }
            for operation in plan.operations
        )
        analysis_identity = {
            "schema": "rosh-analysis-identity/v1",
            "mode": "historical_match",
            "match_id": MATCH_ID,
            "date_time": DATE_TIME,
            "draft_hash": draft_hash,
            "request_hash": plan.request_hash,
            "profile": {
                "rosh_profile_id": profile.rosh_profile_id,
                "formula_version": profile.formula_version,
                "request_profile_hash": profile.request_profile_hash,
                "upstream_bundle_hash": profile.upstream_bundle_hash,
                "scorer_source_hash": profile.scorer_source_hash,
                "canonical_profile_hash": profile.canonical_profile_hash,
                "serialization_version": profile.serialization_version,
            },
        }
        evidence_hash = _hash(
            {
                "schema": "rosh-analysis-evidence/v1",
                "analysis_identity": analysis_identity,
                "request_artifact_hash": request_receipt.content_sha256,
                "response_artifact_hash": response_receipt.content_sha256,
                "result_hash": result.result_hash,
                "status": "succeeded",
            }
        )
        run_id = _hash(
            {
                "schema": "rosh-analysis-run-id/v1",
                "evidence_hash": evidence_hash,
                "status": "succeeded",
            }
        )
        run = RoshRunRecord(
            run_id=run_id,
            status="succeeded",
            mode="historical_match",
            match_id=MATCH_ID,
            date_time=DATE_TIME,
            draft_hash=draft_hash,
            draft=draft,
            rosh_profile_id=profile.rosh_profile_id,
            formula_version=profile.formula_version,
            request_profile_hash=profile.request_profile_hash,
            upstream_bundle_hash=profile.upstream_bundle_hash,
            scorer_source_hash=profile.scorer_source_hash,
            canonical_profile_hash=profile.canonical_profile_hash,
            serialization_version=profile.serialization_version,
            request_hash=plan.request_hash,
            request_manifest=request_manifest,
            response_manifest=response_manifest,
            evidence_hash=evidence_hash,
            collected_at=collected_at,
            radiant_team_score=result.radiant_team_score,
            dire_team_score=result.dire_team_score,
            relative_advantage=result.relative_advantage,
        )
        repository.write_succeeded(run, heroes, minutes)
        lineage = {
            "schema": ROSH_BRIDGE_LINEAGE_SCHEMA,
            "run_id": run_id,
            "source": "opendota",
            "source_match_id": str(MATCH_ID),
            "map_number": 1,
            "request_started_at": GENERATED_AT.isoformat().replace("+00:00", "Z"),
            "generated_at": GENERATED_AT.isoformat().replace("+00:00", "Z"),
            "available_at": collected_at,
            "input_artifact_hash": request_receipt.content_sha256,
            "response_artifact_hash": response_receipt.content_sha256,
        }
        lineage["content_hash"] = _hash(lineage)
        evidence = {"authority_bridge": lineage}
        score_key = _hash({"domain": "legacy-rosh-verification/v1", "index": index})
        session.execute(
            """INSERT INTO historical_rosh_lineup_scores
               (score_key, match_id, radiant_hero_ids_json, dire_hero_ids_json,
                radiant_player_ids_json, dire_player_ids_json,
                pure_lineup_score, current_player_adjusted_lineup_score,
                effective_lineup_score, scoring_mode, player_coverage_count,
                source_name, source_week, source_as_of, player_stats_as_of,
                formula_version, evidence_json, evidence_hash,
                backtest_eligible, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 'pure',
                       0, 'stratz', ?, ?, NULL, ?, ?, ?, 0, ?)""",
            (
                score_key,
                MATCH_ID,
                json.dumps(RADIANT),
                json.dumps(DIRE),
                json.dumps([None] * 5),
                json.dumps([None] * 5),
                result.relative_advantage,
                result.relative_advantage,
                DATE_TIME,
                GENERATED_AT.isoformat(),
                profile.formula_version,
                json.dumps(evidence, separators=(",", ":"), sort_keys=True),
                _legacy_hash(evidence),
                AVAILABLE_AT.isoformat(),
            ),
        )


def _counts(url: str) -> dict[str, int]:
    engine = build_engine(url)
    try:
        with engine.connect() as connection:
            result: dict[str, int] = {}
            for table in (
                "historical_rosh_lineup_scores",
                "rosh_analysis_runs",
                "rosh_run_match_links",
            ):
                result[table] = int(
                    connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                )
            return result
    finally:
        engine.dispose()


def verify(database_url: str) -> dict[str, object]:
    base_url = make_url(require_database_url(database_url))
    source_url = base_url.render_as_string(hide_password=False)
    source_before = _counts(source_url)
    database_name = f"dota2_rosh_bridge_validation_{uuid4().hex}"
    admin_engine = create_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    test_url = base_url.set(database=database_name).render_as_string(
        hide_password=False
    )
    engine = None
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", test_url)
        command.upgrade(config, "head")
        engine = build_engine(test_url)
        session = PostgresSession(engine)
        with tempfile.TemporaryDirectory(prefix="rosh-bridge-validation-") as temporary:
            artifact_root = Path(temporary) / "artifacts"
            with session.transaction():
                _seed_match_authority(session)
                _seed_runs_and_legacy(session, artifact_root, 100)
            targets = {MATCH_ID: _target()}
            report_20 = audit_rosh_authority_bridge(
                session,
                artifact_root=artifact_root,
                max_rows=20,
                created_at=AVAILABLE_AT,
                draft_targets=targets,
            )
            if report_20.stages[-1].support != 20:
                raise RuntimeError("20-row bridge audit did not remain fully eligible")
            diagnostics_20 = {
                row.reason: row.support
                for row in report_20.player_identity_diagnostics
            }
            if report_20.player_identity_support != 0 or diagnostics_20 != {
                "player_coverage_incomplete": 20,
                "player_ids_unavailable": 20,
            }:
                raise RuntimeError("optional player evidence changed eligibility")

            def interrupt(stage: str, index: int) -> None:
                if stage == "record" and index == 4:
                    raise RuntimeError("injected bridge interruption")

            try:
                persist_rosh_authority_bridge(
                    session, report_20, checkpoint=interrupt
                )
            except RuntimeError as error:
                if str(error) != "injected bridge interruption":
                    raise
            else:
                raise RuntimeError("bridge interruption was not injected")
            rollback_counts = {
                "records": int(
                    session.execute(
                        "SELECT COUNT(*) FROM rosh_authority_bridge_records"
                    ).fetchone()[0]
                ),
                "links": int(
                    session.execute("SELECT COUNT(*) FROM rosh_run_match_links").fetchone()[0]
                ),
            }
            if rollback_counts != {"records": 0, "links": 0}:
                raise RuntimeError("bridge interruption did not roll back atomically")

            first_20 = persist_rosh_authority_bridge(session, report_20)
            repeated_20 = persist_rosh_authority_bridge(session, report_20)
            report_100 = audit_rosh_authority_bridge(
                session,
                artifact_root=artifact_root,
                max_rows=100,
                created_at=AVAILABLE_AT,
                draft_targets=targets,
            )
            if report_100.stages[-1].support != 100:
                raise RuntimeError("100-row bridge audit did not remain fully eligible")
            diagnostics_100 = {
                row.reason: row.support
                for row in report_100.player_identity_diagnostics
            }
            if report_100.player_identity_support != 0 or diagnostics_100 != {
                "player_coverage_incomplete": 100,
                "player_ids_unavailable": 100,
            }:
                raise RuntimeError("100-row optional player audit disagrees")
            final_100 = persist_rosh_authority_bridge(session, report_100)
            first_snapshot = replay_rosh_authority_bridge_record(
                session,
                report_100.eligible_records[0],
                artifact_root=artifact_root,
            )
            last_snapshot = replay_rosh_authority_bridge_record(
                session,
                report_100.eligible_records[-1],
                artifact_root=artifact_root,
            )
            hash_mismatch_rejected = False
            try:
                replay_rosh_authority_bridge_record(
                    session,
                    replace(
                        report_100.eligible_records[0],
                        content_hash="0" * 64,
                    ),
                    artifact_root=artifact_root,
                )
            except ValueError as error:
                hash_mismatch_rejected = "content hash" in str(error)
            if not hash_mismatch_rejected:
                raise RuntimeError("tampered R.O.S.H. bridge hash was accepted")
            append_only_rejected = False
            try:
                session.execute(
                    "DELETE FROM rosh_authority_bridge_records WHERE bridge_key=?",
                    (report_100.eligible_records[0].bridge_key,),
                )
            except Exception:
                append_only_rejected = True
            if not append_only_rejected:
                raise RuntimeError("append-only bridge DELETE was accepted")
            final_counts = {
                "records": int(
                    session.execute(
                        "SELECT COUNT(*) FROM rosh_authority_bridge_records"
                    ).fetchone()[0]
                ),
                "links": int(
                    session.execute("SELECT COUNT(*) FROM rosh_run_match_links").fetchone()[0]
                ),
            }
            optional_player_rows = int(
                session.execute(
                    """SELECT COUNT(*)
                         FROM rosh_authority_bridge_records
                        WHERE radiant_player_ids_json IS NULL
                          AND dire_player_ids_json IS NULL
                          AND player_coverage_count = 0"""
                ).fetchone()[0]
            )
            if optional_player_rows != 100:
                raise RuntimeError("optional player fields were not persisted canonically")
            session.close()
            result = {
                "database": database_name,
                "twenty": {
                    "eligible": report_20.stages[-1].support,
                    "player_identity_support": report_20.player_identity_support,
                    "player_identity_diagnostics": diagnostics_20,
                    "inserted": first_20.inserted_records,
                    "repeat_unchanged": repeated_20.unchanged_records,
                    "rollback": rollback_counts,
                },
                "hundred": {
                    "eligible": report_100.stages[-1].support,
                    "player_identity_support": report_100.player_identity_support,
                    "player_identity_diagnostics": diagnostics_100,
                    "optional_player_rows": optional_player_rows,
                    "inserted": final_100.inserted_records,
                    "unchanged": final_100.unchanged_records,
                    "final_counts": final_counts,
                },
                "replay": {
                    "first_status": first_snapshot.status,
                    "last_status": last_snapshot.status,
                    "same_result_hash": (
                        first_snapshot.result_hash == last_snapshot.result_hash
                    ),
                    "hash_mismatch_rejected": hash_mismatch_rejected,
                },
                "append_only_delete_rejected": append_only_rejected,
            }
    finally:
        if engine is not None:
            engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:database_name AND pid<>pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE "{database_name}"'))
        admin_engine.dispose()
    source_after = _counts(source_url)
    result["source_before"] = source_before
    result["source_after"] = source_after
    result["source_unchanged"] = source_before == source_after
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="PostgreSQL URL (default: DATABASE_URL)")
    parser.add_argument("--json-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = verify(require_database_url(args.database_url))
    payload = json.dumps(result, allow_nan=False, ensure_ascii=True, sort_keys=True)
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(payload + "\n", encoding="utf-8", newline="\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
