"""Read-only support funnel for historical and official R.O.S.H. evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from database.session import PostgresSession
from live_betting.rosh_parity_storage import (
    RoshRunMatchLink,
    RoshRunRepository,
    StoredRoshRun,
)

from .backtest import DraftCorpus, load_draft_corpus
from .draft_features import ROLE_CONFIDENCE_MIN, AvailabilityMode, DraftTeam
from .roles import RECONSTRUCTED_ASSIGNMENT_VERSION
from .rosh_features import (
    RoshFeatureTarget,
    build_rosh_feature_snapshot_with_authority,
)


ROSH_SUPPORT_FUNNEL_VERSION = "rosh-support-funnel-v1"


@dataclass(frozen=True)
class RoshFunnelStage:
    stage: str
    support: int


@dataclass(frozen=True)
class RoshMissingReasonCount:
    reason: str
    support: int


@dataclass(frozen=True)
class RoshSupportFunnelReport:
    version: str
    formal_maps: int
    draft_role_ready_targets: int
    draft_exact_position_targets: int
    official_runs: int
    official_match_links: int
    official_formal_match_links: int
    legacy_funnel: tuple[RoshFunnelStage, ...]
    snapshot_attempts: int
    snapshot_available: int
    snapshot_missing_reasons: tuple[RoshMissingReasonCount, ...]
    blocking_stage: str


def _load_rosh_authority(
    connection: PostgresSession,
) -> tuple[tuple[StoredRoshRun, ...], tuple[RoshRunMatchLink, ...]]:
    repository = RoshRunRepository(connection)
    run_ids = tuple(
        str(row["run_id"])
        for row in connection.execute(
            "SELECT run_id FROM rosh_analysis_runs ORDER BY run_id"
        ).fetchall()
    )
    runs: list[StoredRoshRun] = []
    links: list[RoshRunMatchLink] = []
    for run_id in run_ids:
        stored = repository.get(run_id)
        if stored is None:
            raise ValueError(f"R.O.S.H. run authority disappeared: {run_id}")
        runs.append(stored)
        links.extend(repository.get_match_links(run_id))
    return tuple(runs), tuple(links)


def _five_positive_ids(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(parsed, list)
        or len(parsed) != 5
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in parsed
        )
        or len(set(parsed)) != 5
    ):
        return None
    return tuple(parsed)


def _heroes_by_expected_position(team: DraftTeam) -> tuple[int, ...] | None:
    by_position: dict[int, int] = {}
    for player in team.players:
        position = player.expected_position
        if (
            position is None
            or player.expected_position_confidence < ROLE_CONFIDENCE_MIN
            or position in by_position
        ):
            return None
        by_position[position] = player.hero_id
    if set(by_position) != set(range(1, 6)):
        return None
    return tuple(by_position[position] for position in range(1, 6))


def _formal_match_ids(connection: PostgresSession) -> set[int]:
    return {
        int(row["match_id"])
        for row in connection.execute(
            "SELECT match_id FROM formal_map_eligibility"
        ).fetchall()
    }


def _legacy_rows(connection: PostgresSession) -> tuple[dict[str, object], ...]:
    return tuple(
        {key: row[key] for key in row.keys()}
        for row in connection.execute(
            """SELECT match_id, radiant_hero_ids_json, dire_hero_ids_json,
                      radiant_player_ids_json, dire_player_ids_json,
                      player_coverage_count, backtest_eligible
                 FROM historical_rosh_lineup_scores
                ORDER BY match_id, score_key"""
        ).fetchall()
    )


def _official_match_ids(
    runs: Iterable[StoredRoshRun],
    links: Iterable[RoshRunMatchLink],
) -> set[int]:
    result = {
        int(stored.run.match_id)
        for stored in runs
        if stored.run.match_id is not None and stored.run.match_id > 0
    }
    for link in links:
        if link.source_match_id.isdigit():
            result.add(int(link.source_match_id))
    return result


def _linked_match_ids(links: Iterable[RoshRunMatchLink]) -> set[int]:
    return {
        int(link.source_match_id)
        for link in links
        if link.source_match_id.isdigit()
    }


def _legacy_funnel(
    rows: tuple[dict[str, object], ...],
    *,
    formal_match_ids: set[int],
    exact_position_ids: set[int],
    official_match_ids: set[int],
) -> tuple[RoshFunnelStage, ...]:
    current = list(rows)
    stages = [RoshFunnelStage("historical_rows", len(current))]
    current = [row for row in current if int(row["match_id"]) in formal_match_ids]
    stages.append(RoshFunnelStage("formal_map_linked", len(current)))
    current = [
        row
        for row in current
        if (
            (radiant := _five_positive_ids(row["radiant_hero_ids_json"]))
            is not None
            and (dire := _five_positive_ids(row["dire_hero_ids_json"])) is not None
            and len(set((*radiant, *dire))) == 10
        )
    ]
    stages.append(RoshFunnelStage("ten_heroes_complete", len(current)))
    current = [row for row in current if int(row["match_id"]) in exact_position_ids]
    stages.append(RoshFunnelStage("ten_expected_positions_complete", len(current)))
    current = [
        row
        for row in current
        if row["player_coverage_count"] == 10
        and (radiant := _five_positive_ids(row["radiant_player_ids_json"]))
        is not None
        and (dire := _five_positive_ids(row["dire_player_ids_json"])) is not None
        and len(set((*radiant, *dire))) == 10
    ]
    stages.append(RoshFunnelStage("player_coverage_complete", len(current)))
    current = [row for row in current if row["backtest_eligible"] in (1, True)]
    stages.append(RoshFunnelStage("legacy_backtest_eligible", len(current)))
    current = [row for row in current if int(row["match_id"]) in official_match_ids]
    stages.append(RoshFunnelStage("official_run_authority_linked", len(current)))
    return tuple(stages)


def build_rosh_support_funnel(
    connection: PostgresSession,
    *,
    artifact_root: str | Path,
) -> RoshSupportFunnelReport:
    """Trace support without mutating role, R.O.S.H., or model authority."""

    if not isinstance(connection, PostgresSession):
        raise ValueError("connection must be a PostgresSession")
    draft_corpus: DraftCorpus = load_draft_corpus(
        connection,
        availability_mode=AvailabilityMode.RECONSTRUCTED,
        assignment_version=RECONSTRUCTED_ASSIGNMENT_VERSION,
    )
    runs, links = _load_rosh_authority(connection)
    formal_ids = _formal_match_ids(connection)
    legacy_rows = _legacy_rows(connection)
    exact_targets: list[tuple[object, tuple[int, ...], tuple[int, ...]]] = []
    for loaded in draft_corpus.targets:
        target = loaded.target
        assert target is not None
        radiant = _heroes_by_expected_position(target.radiant)
        dire = _heroes_by_expected_position(target.dire)
        if radiant is not None and dire is not None:
            exact_targets.append((target, radiant, dire))
    exact_ids = {int(target.match_id) for target, _radiant, _dire in exact_targets}
    official_ids = _official_match_ids(runs, links)
    reasons: dict[str, int] = {}
    available = 0
    for target, radiant, dire in exact_targets:
        snapshot, _authority = build_rosh_feature_snapshot_with_authority(
            RoshFeatureTarget(
                match_id=target.match_id,
                date_time=int(target.prediction_cutoff.timestamp()),
                prediction_cutoff=target.prediction_cutoff,
                availability_mode=target.availability_mode.value,
                radiant_hero_ids=radiant,
                dire_hero_ids=dire,
            ),
            runs,
            artifact_root=artifact_root,
            match_links=links,
        )
        if snapshot.status == "available":
            available += 1
        else:
            reason = snapshot.missing_reason or "unknown"
            reasons[reason] = reasons.get(reason, 0) + 1
    official_formal_link_ids = _linked_match_ids(links) & formal_ids
    legacy_funnel = _legacy_funnel(
        legacy_rows,
        formal_match_ids=formal_ids,
        exact_position_ids=exact_ids,
        official_match_ids=official_ids,
    )
    blocking_stage = next(
        (
            current.stage
            for previous, current in zip(legacy_funnel, legacy_funnel[1:])
            if current.support == 0 or current.support < previous.support / 2
        ),
        "snapshot_replay",
    )
    return RoshSupportFunnelReport(
        version=ROSH_SUPPORT_FUNNEL_VERSION,
        formal_maps=len(formal_ids),
        draft_role_ready_targets=len(draft_corpus.targets),
        draft_exact_position_targets=len(exact_targets),
        official_runs=len(runs),
        official_match_links=len(links),
        official_formal_match_links=len(official_formal_link_ids),
        legacy_funnel=legacy_funnel,
        snapshot_attempts=len(exact_targets),
        snapshot_available=available,
        snapshot_missing_reasons=tuple(
            RoshMissingReasonCount(reason, support)
            for reason, support in sorted(
                reasons.items(), key=lambda item: (-item[1], item[0])
            )
        ),
        blocking_stage=blocking_stage,
    )


def report_as_dict(report: RoshSupportFunnelReport) -> dict[str, Any]:
    return asdict(report)


def report_as_markdown(report: RoshSupportFunnelReport) -> str:
    lines = [
        "# R.O.S.H. Support Funnel",
        "",
        f"- Version: `{report.version}`",
        f"- Formal maps: {report.formal_maps}",
        f"- Draft role-ready targets: {report.draft_role_ready_targets}",
        f"- Draft exact-position targets: {report.draft_exact_position_targets}",
        f"- Official runs: {report.official_runs}",
        f"- Official match links: {report.official_match_links}",
        f"- Official links to formal maps: {report.official_formal_match_links}",
        f"- Blocking stage: `{report.blocking_stage}`",
        "",
        "## Legacy-to-Official Funnel",
        "",
        "| Stage | Support |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {row.stage} | {row.support} |" for row in report.legacy_funnel)
    lines.extend(
        (
            "",
            "## Current Snapshot Replay",
            "",
            f"- Attempts: {report.snapshot_attempts}",
            f"- Available: {report.snapshot_available}",
            "",
            "| Missing reason | Support |",
            "| --- | ---: |",
        )
    )
    lines.extend(
        f"| {row.reason} | {row.support} |"
        for row in report.snapshot_missing_reasons
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "ROSH_SUPPORT_FUNNEL_VERSION",
    "RoshFunnelStage",
    "RoshMissingReasonCount",
    "RoshSupportFunnelReport",
    "build_rosh_support_funnel",
    "report_as_dict",
    "report_as_markdown",
]
