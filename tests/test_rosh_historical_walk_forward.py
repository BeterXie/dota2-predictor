from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from event_intelligence.rosh_historical_walk_forward import (
    TransportResponse,
    WalkForwardTarget,
    _request_body,
    run_temporal_semantics_audit,
    select_temporal_sample,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "stratz-rosh.json"
UTC = timezone.utc


class FakeTransport:
    def __init__(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        responses = fixture["responses"]
        self.body = json.dumps(
            [
                responses["heroes_meta_positions"],
                responses["hero_stats_by_time_bracket"],
                responses["synergy"],
            ],
            separators=(",", ":"),
        ).encode()
        self.requests: list[bytes] = []

    def fetch(self, request_body: bytes) -> TransportResponse:
        self.requests.append(request_body)
        return TransportResponse(
            queried_at=datetime(2026, 8, 6, tzinfo=UTC),
            status_code=200,
            body=self.body,
        )


class ProvenanceTransport(FakeTransport):
    def fetch(self, request_body: bytes) -> TransportResponse:
        response = super().fetch(request_body)
        request = json.loads(request_body)
        statistics_cutoff = int(request[0]["variables"]["week"])
        payload = json.loads(response.body)
        for item in payload:
            item["data"]["source"] = {
                "sourceMatchId": 999,
                "sourceTimestamp": statistics_cutoff + 1,
            }
        return TransportResponse(
            queried_at=response.queried_at,
            status_code=response.status_code,
            body=json.dumps(payload, separators=(",", ":")).encode(),
        )


class ChangingResponseTransport(FakeTransport):
    def fetch(self, request_body: bytes) -> TransportResponse:
        response = super().fetch(request_body)
        payload = json.loads(response.body)
        if len(self.requests) % 3 == 0:
            payload[0]["extensions"] = {"repeat": len(self.requests)}
        return TransportResponse(
            queried_at=response.queried_at,
            status_code=response.status_code,
            body=json.dumps(payload, separators=(",", ":")).encode(),
        )


def _targets(count: int = 30) -> tuple[WalkForwardTarget, ...]:
    start = datetime(2026, 1, 4, 23, 30, tzinfo=UTC)
    values: list[WalkForwardTarget] = []
    for index in range(count):
        series_id = 100 if index < 3 else 200 + index
        values.append(
            WalkForwardTarget(
                match_id=10_000 + index,
                prediction_cutoff=start + timedelta(hours=index * 3),
                event_id=f"event-{index % 9}",
                patch=56 + (index % 5),
                series_id=series_id,
                series_map_number=index + 1 if index < 3 else 1,
                radiant_expected=(1, 2, 3, 4, 5),
                dire_expected=(6, 7, 8, 9, 10),
                future_series_match_ids=(
                    tuple(10_000 + later for later in range(index + 1, 3))
                    if index < 3
                    else ()
                ),
            )
        )
    return tuple(values)


def test_sample_covers_required_temporal_cases() -> None:
    sample = select_temporal_sample(_targets())

    assert len(sample) == 20
    reasons = {reason for row in sample for reason in row.selection_reasons}
    assert {
        "same_series_maps_1_2_3",
        "week_boundary",
        "multiple_patches",
        "multiple_events",
        "consecutive_matches",
    } <= reasons
    series_maps = {
        row.series_map_number
        for row in sample
        if "same_series_maps_1_2_3" in row.selection_reasons
    }
    assert {1, 2, 3} <= series_maps
    assert len({row.patch for row in sample}) >= 5
    assert len({row.event_id for row in sample}) >= 8


def test_statistics_cutoff_never_exceeds_prediction_cutoff() -> None:
    target = _targets(1)[0]

    with pytest.raises(ValueError, match="must not exceed"):
        _request_body(target, target.prediction_cutoff + timedelta(seconds=1))


def test_20_map_audit_archives_and_replays_but_fails_unprovable_semantics(
    tmp_path: Path,
) -> None:
    sample = select_temporal_sample(_targets())
    transport = FakeTransport()

    report = run_temporal_semantics_audit(
        sample,
        transport=transport,
        artifact_root=tmp_path / "artifacts",
        throttle_seconds=0,
    )

    assert len(transport.requests) == 60
    assert report.audited_maps == 20
    assert report.archived_requests == 60
    assert report.archived_responses == 60
    assert report.archived_normalized_statistics == 60
    assert report.offline_exact_replays == 60
    assert report.temporal_provenance_complete == 0
    assert report.repeated_response_changes == 0
    assert report.gate_passed is False
    assert {
        row.value: row.support for row in report.failure_reasons
    }["aggregate_response_lacks_temporal_match_provenance"] == 20
    assert report.mode == "reconstructed_walk_forward"
    assert report.research_only is True
    assert report.prospective is False
    assert report.deployment_eligible is False

    first = report.maps[0].observations[1]
    request_path = (tmp_path / "artifacts").joinpath(
        *str(first.request_artifact["relative_path"]).split("/")
    )
    request = json.loads(gzip.decompress(request_path.read_bytes()))
    week = int(sample[0].prediction_cutoff.timestamp())
    assert request[0]["variables"]["week"] == week
    assert request[1]["variables"]["week"] == week
    assert request[2]["variables"]["currentWeek"] == week
    assert "endDateTime" not in json.dumps(request)


def test_post_cutoff_source_timestamp_fails_closed(tmp_path: Path) -> None:
    report = run_temporal_semantics_audit(
        select_temporal_sample(_targets()),
        transport=ProvenanceTransport(),
        artifact_root=tmp_path / "artifacts",
        throttle_seconds=0,
    )

    assert report.temporal_provenance_complete == 20
    assert report.gate_passed is False
    assert {
        row.value: row.support for row in report.failure_reasons
    }["source_timestamp_after_statistics_cutoff"] == 20
    assert all(row.source_timestamps_within_cutoff is False for row in report.maps)


def test_repeat_change_compares_exact_response_bytes(tmp_path: Path) -> None:
    report = run_temporal_semantics_audit(
        select_temporal_sample(_targets()),
        transport=ChangingResponseTransport(),
        artifact_root=tmp_path / "artifacts",
        throttle_seconds=0,
    )

    assert report.repeated_response_changes == 20
    assert {
        row.value: row.support for row in report.failure_reasons
    }["repeated_historical_response_changed"] == 20
