"""Read-only verifier for ADR-0002 qualifying strategy rejections."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .comeback import _select_rosh_minute_score
from .draft_authority import authority_from_row, draft_landmark_authority_matches
from .storage import (
    _DRAFT_AUTHORITY_COLUMNS,
    _VISION_AUTHORITY_COLUMNS,
    query_rosh_lineup_score_for_trusted_draft,
)
from .strategy_contract import (
    ReplayResult,
    parse_decision_payload,
    replay_persisted_decision,
)
from .strict_eligibility import query_strict_mapping_snapshot


QUALIFYING_REJECTION_REASONS = frozenset(
    {
        "odds_outside_range",
        "market_not_stable_two_snapshots",
        "vision_situation_collapsed",
        "underdog_deficit_not_material",
        "comeback_entry_outside_time_window",
        "rosh_direction_opposes_underdog",
        "draft_landmark_support_or_calibration_failed",
        "insufficient_data_quality",
        "no_independent_positive_contribution",
        "edge_below_threshold",
        "conservative_probability_not_above_market",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class M1Verification:
    decision_key: str
    m1_qualifying_rejection: bool
    reason: str
    strategy_version: str | None = None
    evaluator_hash: str | None = None
    policy_hash: str | None = None
    serialization_version: str | None = None
    replay_reason: str | None = None


def _result(
    decision_key: str,
    qualifying: bool,
    reason: str,
    *,
    row: Mapping[str, Any] | None = None,
    replay: ReplayResult | None = None,
) -> M1Verification:
    contract = replay.contract if replay is not None else None
    return M1Verification(
        decision_key=decision_key,
        m1_qualifying_rejection=qualifying,
        reason=reason,
        strategy_version=(str(row["strategy_version"]) if row is not None else None),
        evaluator_hash=(contract.evaluator_hash if contract else None),
        policy_hash=(contract.policy_hash if contract else None),
        serialization_version=(contract.serialization_version if contract else None),
        replay_reason=(replay.reason if replay is not None else None),
    )


def _sha(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _profile_authority_complete(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    inputs: Mapping[str, Any],
) -> bool:
    try:
        decision_time = datetime.fromisoformat(str(row["decided_at"]))
        mapping_id = int(row["draft_strict_mapping_id"])
    except (TypeError, ValueError):
        return False
    mapping = query_strict_mapping_snapshot(
        connection, mapping_id=mapping_id, observed_at=decision_time
    )
    if not mapping.eligible or mapping.mapping is None:
        return False
    canonical = inputs.get("canonical_evaluator_inputs")
    if not isinstance(canonical, Mapping):
        return False
    underdog_side = str(row["underdog_side"])
    expected_profiles = {
        "team_one_intelligence": (
            mapping.mapping.canonical_team_one_id,
            canonical.get(
                "underdog_style" if underdog_side == "team_one" else "favorite_style"
            ),
            canonical.get(
                "underdog_form" if underdog_side == "team_one" else "favorite_form"
            ),
        ),
        "team_two_intelligence": (
            mapping.mapping.canonical_team_two_id,
            canonical.get(
                "underdog_style" if underdog_side == "team_two" else "favorite_style"
            ),
            canonical.get(
                "underdog_form" if underdog_side == "team_two" else "favorite_form"
            ),
        ),
    }
    for name, (expected_team_id, evaluator_style, evaluator_form) in expected_profiles.items():
        value = inputs.get(name)
        if not isinstance(value, Mapping) or set(value) != {"team_style", "player_form"}:
            return False
        style = value.get("team_style")
        form = value.get("player_form")
        if (
            not isinstance(style, Mapping)
            or "status" in style
            or style.get("team_id") != expected_team_id
            or not isinstance(style.get("profile_cutoff"), str)
            or not isinstance(style.get("profile_version"), str)
            or not _sha(style.get("input_hash"))
            or isinstance(style.get("effective_sample_size"), bool)
            or not isinstance(style.get("effective_sample_size"), (int, float))
            or float(style["effective_sample_size"]) <= 0.0
            or not isinstance(evaluator_style, Mapping)
            or evaluator_style.get("team_id") != expected_team_id
            or not isinstance(form, Mapping)
            or "status" in form
            or not isinstance(form.get("cutoff"), str)
            or not isinstance(evaluator_form, Mapping)
        ):
            return False
        account_ids = form.get("account_ids")
        score_refs = form.get("score_refs")
        if (
            not isinstance(account_ids, list)
            or len(account_ids) != 5
            or len(set(account_ids)) != 5
            or any(not isinstance(item, int) or item <= 0 for item in account_ids)
            or evaluator_form.get("account_ids") != account_ids
            or not isinstance(score_refs, list)
            or not score_refs
        ):
            return False
        selected = connection.execute(
            """SELECT team_id, profile_cutoff, profile_version, input_hash,
                      effective_sample_size
                 FROM team_style_profiles
                WHERE team_id=? AND profile_cutoff<=? AND created_at<=?
                  AND profile_version=?
                ORDER BY profile_cutoff DESC, created_at DESC, profile_id DESC
                LIMIT 1""",
            (
                expected_team_id,
                decision_time.isoformat(),
                decision_time.isoformat(),
                style["profile_version"],
            ),
        ).fetchone()
        if (
            selected is None
            or str(selected["profile_cutoff"]) != style["profile_cutoff"]
            or str(selected["profile_version"]) != style["profile_version"]
            or str(selected["input_hash"]) != style["input_hash"]
            or float(selected["effective_sample_size"])
            != float(style["effective_sample_size"])
        ):
            return False
        for score in score_refs:
            if (
                not isinstance(score, Mapping)
                or not isinstance(score.get("match_id"), int)
                or not isinstance(score.get("player_slot"), int)
                or not _sha(score.get("input_hash"))
                or not isinstance(score.get("score_version"), str)
                or not isinstance(score.get("created_at"), str)
            ):
                return False
            persisted = connection.execute(
                """SELECT account_id, input_hash, score_version, created_at
                     FROM player_map_scores
                    WHERE match_id=? AND player_slot=? AND score_version=?""",
                (
                    score["match_id"],
                    score["player_slot"],
                    score["score_version"],
                ),
            ).fetchone()
            if (
                persisted is None
                or persisted["account_id"] not in account_ids
                or str(persisted["input_hash"]) != score["input_hash"]
                or str(persisted["created_at"]) != score["created_at"]
            ):
                return False
    return True


def _rosh_authority_complete(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    inputs: Mapping[str, Any],
) -> bool:
    rosh = inputs.get("rosh_lineup_score")
    if not isinstance(rosh, Mapping) or rosh.get("status") == "unavailable":
        return False
    try:
        decision_time = datetime.fromisoformat(str(row["decided_at"]))
        radiant = tuple(json.loads(str(row["vision_radiant_hero_ids_json"])))
        dire = tuple(json.loads(str(row["vision_dire_hero_ids_json"])))
        score = query_rosh_lineup_score_for_trusted_draft(
            connection,
            raybet_match_id=str(row["raybet_match_id"]),
            map_number=int(row["map_number"]),
            strict_mapping_id=int(row["draft_strict_mapping_id"]),
            draft_hash=str(rosh["draft_hash"]),
            radiant_hero_ids=radiant,
            dire_hero_ids=dire,
            as_of=decision_time,
            formula_version=str(rosh["formula_version"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if score is None:
        return False
    persisted_ref = score.as_input_ref()
    for key in (
        "score_key", "draft_hash", "player_identity_hash", "pure_score",
        "player_adjusted_score", "effective_score", "mode",
        "player_coverage", "player_coverage_count", "stake_cap",
        "formula_version", "source_name", "source_week", "cache_week_start",
        "source_as_of", "evidence_hash", "evidence",
    ):
        if rosh.get(key) != persisted_ref.get(key):
            return False
    selected = _select_rosh_minute_score(
        score, int(row["vision_observed_game_clock_seconds"])
    )
    return bool(
        selected is not None
        and rosh.get("draft_matches_observation") is True
        and rosh.get("selected_table") == selected["table"]
        and rosh.get("selected_minute") == selected["minute"]
        and rosh.get("selected_score") == selected["score"]
        and rosh.get("match_percentage") == selected["match_percentage"]
    )


def _transport_authority_complete(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    inputs: Mapping[str, Any],
) -> bool:
    transport = inputs.get("transport")
    if not isinstance(transport, Mapping):
        return False
    current_key = transport.get("current_key")
    previous_key = transport.get("previous_key")
    current_at = transport.get("current_at")
    previous_at = transport.get("previous_at")
    if (
        not all(isinstance(value, str) and value for value in (current_key, previous_key, current_at, previous_at))
        or current_key == previous_key
    ):
        return False
    try:
        current_time = datetime.fromisoformat(str(current_at))
        previous_time = datetime.fromisoformat(str(previous_at))
    except ValueError:
        return False
    if not previous_time < current_time or str(row["decided_at"]) != str(current_at):
        return False
    rows = connection.execute(
        """SELECT observation_key, raybet_match_id, observed_at, source,
                  timing_status, processing_status, response_state_hash,
                  response_artifact_hash
             FROM odds_transport_observations
            WHERE observation_key IN (?, ?)""",
        (current_key, previous_key),
    ).fetchall()
    by_key = {str(item["observation_key"]): item for item in rows}
    if set(by_key) != {current_key, previous_key}:
        return False
    expected_times = {str(current_key): str(current_at), str(previous_key): str(previous_at)}
    markets: dict[str, Mapping[str, Any]] = {}
    for key, item in by_key.items():
        if (
            str(item["raybet_match_id"]) != str(row["raybet_match_id"])
            or str(item["observed_at"]) != expected_times[key]
            or str(item["source"]) != "direct"
            or str(item["timing_status"]) != "on_time"
            or str(item["processing_status"]) != "processed"
            or not _sha(item["response_state_hash"])
            or not _sha(item["response_artifact_hash"])
        ):
            return False
        winner = connection.execute(
            """SELECT * FROM trusted_odds_winner_market_authority
                WHERE observation_key=? AND raybet_match_id=? AND period=?
                  AND response_state_hash=? AND response_artifact_hash=?""",
            (
                key, row["raybet_match_id"], f"map_{row['map_number']}",
                item["response_state_hash"], item["response_artifact_hash"],
            ),
        ).fetchone()
        if winner is None:
            return False
        markets[key] = winner
    current_market = markets[str(current_key)]
    previous_market = markets[str(previous_key)]
    market_input = inputs.get("market")
    canonical = inputs.get("canonical_evaluator_inputs")
    stability = inputs.get("stability")
    surface = canonical.get("surface") if isinstance(canonical, Mapping) else None
    if (
        not isinstance(market_input, Mapping)
        or not isinstance(surface, Mapping)
        or not isinstance(stability, Mapping)
    ):
        return False
    try:
        current_probability = float(current_market["underdog_probability"])
        previous_probability = float(previous_market["underdog_probability"])
        current_price = float(current_market["underdog_price"])
        tolerance = float(
            stability["maximum_absolute_devigged_probability_move"]
        )
    except (KeyError, TypeError, ValueError):
        return False
    current_side = str(current_market["underdog_side"])
    previous_side = str(previous_market["underdog_side"])
    same_side = current_side == previous_side
    signed_move = current_probability - previous_probability if same_side else 0.0
    actual_move = abs(signed_move) if same_side else None
    expected_stable = same_side and actual_move is not None and actual_move <= tolerance
    persisted_actual = stability.get("actual_absolute_devigged_probability_move")
    return bool(
        current_side == str(row["underdog_side"])
        and math.isclose(
            current_probability, float(row["market_probability"]),
            rel_tol=0.0, abs_tol=1.0e-12,
        )
        and market_input.get("underdog_side") == current_side
        and math.isclose(
            float(market_input.get("underdog_price")), current_price,
            rel_tol=0.0, abs_tol=1.0e-12,
        )
        and math.isclose(
            float(market_input.get("underdog_probability")), current_probability,
            rel_tol=0.0, abs_tol=1.0e-12,
        )
        and surface.get("underdog_side") == current_side
        and math.isclose(
            float(surface.get("underdog_price")), current_price,
            rel_tol=0.0, abs_tol=1.0e-12,
        )
        and math.isclose(
            float(surface.get("underdog_probability")), current_probability,
            rel_tol=0.0, abs_tol=1.0e-12,
        )
        and math.isclose(
            float(surface.get("probability_move")), signed_move,
            rel_tol=0.0, abs_tol=1.0e-12,
        )
        and stability.get("stable") is expected_stable
        and (
            (persisted_actual is None and actual_move is None)
            or (
                persisted_actual is not None
                and actual_move is not None
                and math.isclose(
                    float(persisted_actual), actual_move,
                    rel_tol=0.0, abs_tol=1.0e-12,
                )
            )
        )
    )


def _previous_vision_complete(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    inputs: Mapping[str, Any],
) -> bool:
    previous = inputs.get("previous_vision")
    transport = inputs.get("transport")
    if not isinstance(previous, Mapping) or not isinstance(transport, Mapping):
        return False
    if (
        previous.get("confirmed") is not True
        or previous.get("is_paused") is not False
        or previous.get("screen_state") != "game"
        or previous.get("radiant_team_side") not in {"team_one", "team_two"}
        or not isinstance(previous.get("game_clock_seconds"), int)
    ):
        return False
    candidate = connection.execute(
        """SELECT * FROM vision_observations
            WHERE raybet_match_id=? AND map_number=? AND captured_at=?
              AND source_frame_ref=? AND confirmed=1 AND is_paused=0
              AND screen_state='game'""",
        (
            row["raybet_match_id"],
            row["map_number"],
            previous.get("captured_at"),
            previous.get("source_frame_ref"),
        ),
    ).fetchone()
    if candidate is None:
        return False
    if (
        candidate["game_clock_seconds"] != previous.get("game_clock_seconds")
        or candidate["radiant_team_side"] != previous.get("radiant_team_side")
        or candidate["source_frame_sha256"] is None
        or candidate["source_frame_bytes"] is None
    ):
        return False
    active = connection.execute(
        """SELECT 1 FROM active_vision_frame_artifacts
            WHERE frame_ref=? AND content_sha256=? AND byte_length=?""",
        (
            previous.get("source_frame_ref"),
            candidate["source_frame_sha256"],
            candidate["source_frame_bytes"],
        ),
    ).fetchone()
    if active is None:
        return False
    try:
        captured = datetime.fromisoformat(str(previous.get("captured_at")))
        observed = datetime.fromisoformat(str(transport.get("previous_at")))
    except ValueError:
        return False
    return 0.0 <= (observed - captured).total_seconds() <= 120.0


def _current_authority_complete(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    inputs: Mapping[str, Any],
) -> bool:
    if any(row.get(column) is None for column in (*_DRAFT_AUTHORITY_COLUMNS, *_VISION_AUTHORITY_COLUMNS)):
        return False
    try:
        decision_time = datetime.fromisoformat(str(row["decided_at"]))
        mapping_id = int(row["draft_strict_mapping_id"])
    except (TypeError, ValueError):
        return False
    mapping = query_strict_mapping_snapshot(
        connection, mapping_id=mapping_id, observed_at=decision_time
    )
    if (
        not mapping.eligible
        or mapping.mapping is None
        or mapping.raybet_match_id != str(row["raybet_match_id"])
        or mapping.map_number != int(row["map_number"])
    ):
        return False
    draft_authority = authority_from_row(row)
    if draft_authority is None or not draft_landmark_authority_matches(
        connection,
        draft_authority,
        raybet_match_id=str(row["raybet_match_id"]),
        map_number=int(row["map_number"]),
        strict_mapping_id=mapping_id,
        radiant_hero_ids=None,
        dire_hero_ids=None,
        observed_at=decision_time,
        require_current_revisions=True,
        verify_curve=False,
    ):
        return False
    vision = connection.execute(
        """SELECT 1 FROM trusted_vision_observation_authority
            WHERE raybet_match_id=? AND map_number=? AND captured_at=?
              AND source_frame_ref=? AND source_frame_sha256=?
              AND source_frame_bytes=? AND game_clock_seconds=?
              AND is_paused=0 AND radiant_team_side=?
              AND clock_confidence=? AND draft_confidence=?
              AND screen_state='game' AND confirmed=1""",
        (
            row["vision_raybet_match_id"], row["vision_map_number"],
            row["vision_captured_at"], row["vision_source_frame_ref"],
            row["vision_source_frame_sha256"], row["vision_source_frame_bytes"],
            row["vision_observed_game_clock_seconds"],
            row["vision_radiant_team_side"], row["vision_clock_confidence"],
            row["vision_draft_confidence"],
        ),
    ).fetchone()
    transport = connection.execute(
        """SELECT 1 FROM odds_transport_observations
            WHERE observation_key=? AND raybet_match_id=? AND observed_at=?
              AND source='direct' AND timing_status='on_time'
              AND processing_status='processed'""",
        (
            row["vision_transport_key"], row["raybet_match_id"],
            row["vision_transport_at"],
        ),
    ).fetchone()
    market = connection.execute(
        """SELECT 1 FROM trusted_odds_winner_market_authority
            WHERE observation_key=? AND raybet_match_id=? AND period=?
              AND underdog_side=?
              AND abs(underdog_probability-?)<=1.0e-12""",
        (
            row["vision_transport_key"], row["raybet_match_id"],
            f"map_{row['map_number']}", row["underdog_side"],
            row["market_probability"],
        ),
    ).fetchone()
    if vision is None or transport is None or market is None:
        return False
    landmark = inputs.get("draft_landmark")
    return (
        isinstance(landmark, Mapping)
        and landmark.get("status") == "validated"
        and _sha(landmark.get("model_hash"))
        and _sha(landmark.get("feature_hash"))
        and _sha(landmark.get("calibration_hash"))
    )


def verify_m1_qualifying_rejection(
    connection: sqlite3.Connection,
    decision_key: str,
) -> M1Verification:
    try:
        row_value = connection.execute(
            "SELECT * FROM strategy_decisions WHERE decision_key=?",
            (decision_key,),
        ).fetchone()
    except sqlite3.Error:
        return _result(decision_key, False, "strategy_schema_unavailable")
    if row_value is None:
        return _result(decision_key, False, "strategy_decision_missing")
    row = dict(row_value)
    if int(row["eligible"]) == 1:
        return _result(decision_key, False, "eligible_decision_not_rejection", row=row)
    if str(row["reason"]) not in QUALIFYING_REJECTION_REASONS:
        return _result(decision_key, False, "reason_not_allowlisted", row=row)
    replay = replay_persisted_decision(row)
    if not replay.valid:
        return _result(decision_key, False, "canonical_replay_failed", row=row, replay=replay)
    try:
        payload = parse_decision_payload(
            str(row["contributions_json"]),
            strategy_version=str(row["strategy_version"]),
        )
        inputs = payload["__inputs__"]
    except (KeyError, TypeError, ValueError):
        return _result(decision_key, False, "persisted_inputs_invalid", row=row, replay=replay)
    if not isinstance(inputs, Mapping):
        return _result(decision_key, False, "persisted_inputs_invalid", row=row, replay=replay)
    try:
        if not _profile_authority_complete(connection, row, inputs):
            return _result(decision_key, False, "profile_or_model_refs_incomplete", row=row, replay=replay)
        if not _current_authority_complete(connection, row, inputs):
            return _result(decision_key, False, "current_authority_incomplete", row=row, replay=replay)
        if not _rosh_authority_complete(connection, row, inputs):
            return _result(decision_key, False, "rosh_authority_incomplete", row=row, replay=replay)
        if not _transport_authority_complete(connection, row, inputs):
            return _result(decision_key, False, "transport_authority_incomplete", row=row, replay=replay)
        if not _previous_vision_complete(connection, row, inputs):
            return _result(decision_key, False, "previous_vision_authority_incomplete", row=row, replay=replay)
        conflict = connection.execute(
            """SELECT 1
                 FROM vision_derived_invalidations
                WHERE dependent_type='strategy_decision' AND dependent_key=?
                UNION ALL
               SELECT 1 FROM strict_live_mapping_impacts
                WHERE dependent_type='strategy_decision' AND dependent_key=?
                LIMIT 1""",
            (decision_key, decision_key),
        ).fetchone()
    except sqlite3.Error:
        return _result(decision_key, False, "authority_schema_unavailable", row=row, replay=replay)
    if conflict is not None:
        return _result(decision_key, False, "authority_conflict", row=row, replay=replay)
    return _result(decision_key, True, "qualifying_strategy_rejection", row=row, replay=replay)
