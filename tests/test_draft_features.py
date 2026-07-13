from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from event_intelligence.draft_features import (
    FEATURE_SCHEMA,
    FEATURE_SCHEMA_HASH,
    AvailabilityMode,
    DerivedFactProvenance,
    DraftHeroMapEvidence,
    DraftMapEvidence,
    DraftPlayer,
    DraftStyleRateSnapshot,
    DraftStyleSnapshot,
    DraftTarget,
    DraftTeam,
    DraftTeamMapEvidence,
    ExpectedRoleAssignment,
    build_draft_feature_snapshot,
)
from event_intelligence.models import RolePurpose
from event_intelligence.roles import RoleSource


UTC = timezone.utc
CUTOFF = datetime(2026, 7, 13, 12, tzinfo=UTC)
_DEFAULT = object()


def _provenance(
    seed: int,
    *,
    cutoff: datetime,
    first_usable_at: datetime | None,
    version: str,
) -> DerivedFactProvenance:
    return DerivedFactProvenance(
        cutoff=cutoff,
        first_usable_at=first_usable_at,
        input_hash=f"{seed:064x}",
        version=version,
    )


def _team(
    team_id: int,
    hero_start: int,
    player_start: int,
    *,
    position_confidence: float = 0.9,
) -> DraftTeam:
    return DraftTeam(
        team_id,
        tuple(
            DraftPlayer(
                player_id=player_start + index,
                hero_id=hero_start + index,
                expected_role=ExpectedRoleAssignment(
                    purpose=RolePurpose.EXPECTED_POSITION,
                    source=(
                        RoleSource.HISTORICAL_PATTERN
                        if position_confidence > 0
                        else RoleSource.UNKNOWN
                    ),
                    position=(index + 1 if position_confidence > 0 else None),
                    confidence=position_confidence,
                    provenance=_provenance(
                        team_id * 100 + index,
                        cutoff=CUTOFF - timedelta(hours=2),
                        first_usable_at=CUTOFF - timedelta(hours=1),
                        version="expected-role-v1",
                    ),
                ),
            )
            for index in range(5)
        ),
    )


RADIANT = _team(10, 1, 101)
DIRE = _team(20, 6, 201)


def _historical_team(team: DraftTeam) -> DraftTeam:
    return DraftTeam(
        team.team_id,
        tuple(
            DraftPlayer(player.player_id, player.hero_id)
            for player in team.players
        ),
    )


def _hero_evidence(
    team: DraftTeam,
    *,
    match_id: int,
    completed_at: datetime,
    first_usable_at: datetime | None,
    score_offset: float = 0.0,
    include_metrics: bool = True,
) -> tuple[DraftHeroMapEvidence, ...]:
    return tuple(
        DraftHeroMapEvidence(
            player_id=player.player_id,
            hero_id=player.hero_id,
            observed_position=index + 1,
            observed_position_confidence=0.9,
            observed_role_purpose=RolePurpose.OBSERVED_POSITION,
            observed_role_source=RoleSource.HISTORICAL_PATTERN,
            observed_role_provenance=_provenance(
                match_id * 1_000 + index,
                cutoff=completed_at,
                first_usable_at=first_usable_at,
                version="observed-role-v1",
            ),
            execution_score=50.0 + score_offset + index,
            score_provenance=_provenance(
                match_id * 1_000 + 100 + index,
                cutoff=completed_at,
                first_usable_at=first_usable_at,
                version="player-score-v1",
            ),
            control_seconds=(10.0 + index if include_metrics else None),
            hero_healing=(100.0 + index * 10 if include_metrics else None),
            last_hits=(100.0 + index * 20 if include_metrics else None),
            tower_damage=(200.0 + index * 20 if include_metrics else None),
            net_worth=(5_000.0 + index * 500 if include_metrics else None),
            buyback_count=(index % 2 if include_metrics else None),
        )
        for index, player in enumerate(team.players)
    )


def _map(
    match_id: int,
    *,
    completed_at: datetime | None = None,
    first_usable_at: datetime | None | object = ...,
    radiant_win: bool = True,
    radiant: DraftTeam = RADIANT,
    dire: DraftTeam = DIRE,
    patch: int | None = 60,
    series_id: int | None = 700,
    include_metrics: bool = True,
) -> DraftMapEvidence:
    completed = completed_at or CUTOFF - timedelta(days=match_id)
    usable = (
        completed + timedelta(minutes=5)
        if first_usable_at is ...
        else first_usable_at
    )
    historical_radiant = _historical_team(radiant)
    historical_dire = _historical_team(dire)
    radiant_state = _provenance(
        match_id * 1_000 + 500,
        cutoff=completed,
        first_usable_at=usable,  # type: ignore[arg-type]
        version="team-state-v1",
    )
    dire_state = _provenance(
        match_id * 1_000 + 501,
        cutoff=completed,
        first_usable_at=usable,  # type: ignore[arg-type]
        version="team-state-v1",
    )
    return DraftMapEvidence(
        evidence_id=f"evidence-{match_id}",
        source_input_hash=f"{match_id:064x}",
        match_id=match_id,
        completed_at=completed,
        first_usable_at=usable,  # type: ignore[arg-type]
        event_id="event-a",
        patch=patch,
        duration_seconds=2_700,
        radiant=historical_radiant,
        dire=historical_dire,
        radiant_win=radiant_win,
        series_id=series_id,
        map_number=match_id,
        radiant_hero_evidence=_hero_evidence(
            historical_radiant,
            match_id=match_id,
            completed_at=completed,
            first_usable_at=usable,  # type: ignore[arg-type]
            score_offset=5.0,
            include_metrics=include_metrics,
        ),
        dire_hero_evidence=_hero_evidence(
            historical_dire,
            match_id=match_id,
            completed_at=completed,
            first_usable_at=usable,  # type: ignore[arg-type]
            score_offset=-5.0,
            include_metrics=include_metrics,
        ),
        radiant_team_evidence=DraftTeamMapEvidence(
            comeback_opportunity=True,
            came_back=radiant_win,
            throw_opportunity=True,
            threw=not radiant_win,
            closeout_opportunity=True,
            closed_out=radiant_win,
            roshan_events=2,
            high_ground_events=2,
            long_fight_wins=2 if radiant_win else 1,
            long_fight_opportunities=3,
            state_provenance=radiant_state,
        ),
        dire_team_evidence=DraftTeamMapEvidence(
            comeback_opportunity=True,
            came_back=not radiant_win,
            throw_opportunity=True,
            threw=radiant_win,
            closeout_opportunity=True,
            closed_out=not radiant_win,
            roshan_events=1,
            high_ground_events=1,
            long_fight_wins=1 if radiant_win else 2,
            long_fight_opportunities=3,
            state_provenance=dire_state,
        ),
    )


def _style(
    team_id: int,
    *,
    mode: AvailabilityMode,
    radiant: bool,
    cutoff: datetime | None = None,
    first_usable_at: datetime | None | object = _DEFAULT,
) -> DraftStyleSnapshot:
    profile_cutoff = cutoff or CUTOFF - timedelta(hours=2)
    usable = (
        profile_cutoff + timedelta(minutes=1)
        if first_usable_at is _DEFAULT
        else first_usable_at
    )
    return DraftStyleSnapshot(
        team_id=team_id,
        availability_mode=mode,
        provenance=_provenance(
            team_id * 10_000,
            cutoff=profile_cutoff,
            first_usable_at=usable,  # type: ignore[arg-type]
            version="team-style-v1",
        ),
        comeback_rate=DraftStyleRateSnapshot(
            value=0.7 if radiant else 0.3,
            support=20,
            coverage=1.0,
        ),
        throw_resilience_rate=DraftStyleRateSnapshot(
            value=0.8 if radiant else 0.4,
            support=20,
            coverage=1.0,
        ),
        closeout_rate=DraftStyleRateSnapshot(
            value=0.75 if radiant else 0.35,
            support=20,
            coverage=1.0,
        ),
    )


def _target(
    *,
    mode: AvailabilityMode = AvailabilityMode.PROSPECTIVE,
    radiant: DraftTeam = RADIANT,
    dire: DraftTeam = DIRE,
    series_id: int | None = 700,
    radiant_style: DraftStyleSnapshot | None | object = _DEFAULT,
    dire_style: DraftStyleSnapshot | None | object = _DEFAULT,
) -> DraftTarget:
    style_first_usable = None if mode is AvailabilityMode.RECONSTRUCTED else _DEFAULT
    return DraftTarget(
        match_id=9_001,
        prediction_cutoff=CUTOFF,
        event_id="event-a",
        patch=60,
        radiant=radiant,
        dire=dire,
        availability_mode=mode,
        series_id=series_id,
        map_number=3,
        radiant_style=(
            _style(
                radiant.team_id,
                mode=mode,
                radiant=True,
                first_usable_at=style_first_usable,
            )
            if radiant_style is _DEFAULT
            else radiant_style
        ),
        dire_style=(
            _style(
                dire.team_id,
                mode=mode,
                radiant=False,
                first_usable_at=style_first_usable,
            )
            if dire_style is _DEFAULT
            else dire_style
        ),
    )


class DraftFeatureCausalityTests(unittest.TestCase):
    def test_future_unavailable_and_cutoff_equal_rows_cannot_change_snapshot(self) -> None:
        base = [_map(1)]
        future = _map(
            2,
            completed_at=CUTOFF + timedelta(seconds=1),
            first_usable_at=CUTOFF + timedelta(minutes=50),
        )
        cutoff_equal = _map(
            3,
            completed_at=CUTOFF,
            first_usable_at=CUTOFF,
        )
        late = _map(
            4,
            completed_at=CUTOFF - timedelta(hours=2),
            first_usable_at=CUTOFF + timedelta(seconds=1),
        )
        unavailable = _map(5, first_usable_at=None)

        before = build_draft_feature_snapshot(_target(), base)
        after = build_draft_feature_snapshot(
            _target(), [late, future, base[0], unavailable, cutoff_equal]
        )

        self.assertEqual(before, after)

    def test_target_map_postmatch_mutations_are_always_excluded(self) -> None:
        base = _map(9_001, completed_at=CUTOFF - timedelta(hours=1))
        target_row = replace(
            base,
            radiant_win=False,
            radiant_hero_evidence=tuple(
                replace(row, execution_score=0.0, control_seconds=99_999.0)
                for row in base.radiant_hero_evidence
            ),
        )

        empty = build_draft_feature_snapshot(_target(), [])
        mutated = build_draft_feature_snapshot(_target(), [target_row])

        self.assertEqual(empty, mutated)
        self.assertEqual(mutated.support, 0)

    def test_reconstructed_and_prospective_availability_are_separate(self) -> None:
        no_timestamp = _map(1, first_usable_at=None)

        prospective = build_draft_feature_snapshot(_target(), [no_timestamp])
        reconstructed = build_draft_feature_snapshot(
            _target(mode=AvailabilityMode.RECONSTRUCTED), [no_timestamp]
        )

        self.assertEqual(prospective.support, 0)
        self.assertEqual(reconstructed.support, 1)
        self.assertNotEqual(prospective.input_hash, reconstructed.input_hash)
        self.assertEqual(
            reconstructed.availability_mode, AvailabilityMode.RECONSTRUCTED
        )

    def test_history_order_duplicates_and_future_mutations_do_not_change_hash(self) -> None:
        first = _map(1)
        second = _map(2, radiant_win=False)
        baseline = build_draft_feature_snapshot(_target(), [first, second])
        reordered = build_draft_feature_snapshot(
            _target(), [second, first, first]
        )
        changed_future = replace(
            _map(
                10,
                completed_at=CUTOFF + timedelta(days=1),
                first_usable_at=CUTOFF + timedelta(days=1, minutes=1),
            ),
            radiant_win=False,
        )

        self.assertEqual(baseline, reordered)
        self.assertEqual(
            baseline,
            build_draft_feature_snapshot(_target(), [first, second, changed_future]),
        )
        self.assertEqual(len(baseline.input_hash), 64)

    def test_past_source_mutation_changes_input_hash(self) -> None:
        row = _map(1)
        changed = replace(row, source_input_hash="f" * 64)

        first = build_draft_feature_snapshot(_target(), [row])
        second = build_draft_feature_snapshot(_target(), [changed])

        self.assertNotEqual(first.input_hash, second.input_hash)

    def test_unavailable_derived_role_score_and_state_are_removed_before_hash(self) -> None:
        row = _map(1)
        future = CUTOFF + timedelta(seconds=1)

        def delayed(
            *,
            role_position: int,
            execution_score: float,
            came_back: bool,
        ) -> DraftMapEvidence:
            radiant_heroes = tuple(
                replace(
                    fact,
                    observed_position=role_position,
                    execution_score=execution_score,
                    observed_role_provenance=replace(
                        fact.observed_role_provenance,
                        first_usable_at=future,
                    ),
                    score_provenance=replace(
                        fact.score_provenance,
                        first_usable_at=future,
                    ),
                )
                for fact in row.radiant_hero_evidence
            )
            dire_heroes = tuple(
                replace(
                    fact,
                    observed_position=role_position,
                    execution_score=execution_score,
                    observed_role_provenance=replace(
                        fact.observed_role_provenance,
                        first_usable_at=future,
                    ),
                    score_provenance=replace(
                        fact.score_provenance,
                        first_usable_at=future,
                    ),
                )
                for fact in row.dire_hero_evidence
            )
            return replace(
                row,
                radiant_hero_evidence=radiant_heroes,
                dire_hero_evidence=dire_heroes,
                radiant_team_evidence=replace(
                    row.radiant_team_evidence,
                    came_back=came_back,
                    roshan_events=99 if came_back else 1,
                    state_provenance=replace(
                        row.radiant_team_evidence.state_provenance,
                        first_usable_at=future,
                    ),
                ),
                dire_team_evidence=replace(
                    row.dire_team_evidence,
                    came_back=not came_back,
                    roshan_events=1 if came_back else 99,
                    state_provenance=replace(
                        row.dire_team_evidence.state_provenance,
                        first_usable_at=future,
                    ),
                ),
            )

        first_row = delayed(role_position=1, execution_score=0.0, came_back=True)
        second_row = delayed(role_position=5, execution_score=100.0, came_back=False)
        first = build_draft_feature_snapshot(_target(), (first_row,))
        second = build_draft_feature_snapshot(_target(), (second_row,))

        self.assertEqual(first, second)
        self.assertEqual(first.feature("role_fit_win_rate_diff").support, 0)
        self.assertIsNone(first.feature("context_player_form_diff").value)
        self.assertIsNone(first.feature("roshan_proxy_diff").value)

        reconstructed_first = build_draft_feature_snapshot(
            _target(mode=AvailabilityMode.RECONSTRUCTED), (first_row,)
        )
        reconstructed_second = build_draft_feature_snapshot(
            _target(mode=AvailabilityMode.RECONSTRUCTED), (second_row,)
        )
        self.assertNotEqual(reconstructed_first, reconstructed_second)

    def test_target_roles_require_causal_expected_assignment_provenance(self) -> None:
        role = RADIANT.players[0].expected_role
        self.assertIsNotNone(role)
        with self.assertRaisesRegex(ValueError, "expected_position purpose"):
            replace(role, purpose=RolePurpose.OBSERVED_POSITION)
        with self.assertRaisesRegex(ValueError, "single_map"):
            replace(role, source=RoleSource.SINGLE_MAP)

        future_role = replace(
            role,
            provenance=replace(
                role.provenance,
                first_usable_at=CUTOFF + timedelta(seconds=1),
            ),
        )
        future_team = replace(
            RADIANT,
            players=(
                replace(RADIANT.players[0], expected_role=future_role),
                *RADIANT.players[1:],
            ),
        )
        with self.assertRaisesRegex(ValueError, "expected role was not available"):
            _target(radiant=future_team)
        with self.assertRaisesRegex(ValueError, "requires an expected-role"):
            _target(radiant=_historical_team(RADIANT))

    def test_future_or_missing_style_snapshot_cannot_enter_prospective_target(self) -> None:
        style = _style(10, mode=AvailabilityMode.PROSPECTIVE, radiant=True)
        future = replace(
            style,
            provenance=replace(
                style.provenance,
                first_usable_at=CUTOFF + timedelta(seconds=1),
            ),
        )
        with self.assertRaisesRegex(ValueError, "style was not available"):
            _target(radiant_style=future)

        target = _target(radiant_style=None, dire_style=None)
        snapshot = build_draft_feature_snapshot(target, (_map(1),))
        for name in (
            "context_comeback_rate_diff",
            "context_throw_resilience_diff",
            "context_closeout_rate_diff",
        ):
            feature = snapshot.feature(name)
            self.assertIsNone(feature.value)
            self.assertEqual(
                feature.missing_reason,
                "causal_team_style_snapshot_unavailable",
            )

    def test_source_hash_must_be_sha256(self) -> None:
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            replace(_map(1), source_input_hash="not-a-hash")

    def test_provenance_availability_cannot_precede_its_cutoff(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot precede cutoff"):
            _provenance(
                1,
                cutoff=CUTOFF,
                first_usable_at=CUTOFF - timedelta(microseconds=1),
                version="derived-v1",
            )

    def test_sha256_identities_are_normalized_before_snapshot_hashing(self) -> None:
        lower = replace(_map(1), source_input_hash="a" * 64)
        upper = replace(_map(1), source_input_hash="A" * 64)
        lower_provenance = _provenance(
            10,
            cutoff=CUTOFF - timedelta(hours=2),
            first_usable_at=CUTOFF - timedelta(hours=1),
            version="derived-v1",
        )
        upper_provenance = replace(lower_provenance, input_hash="A" * 64)

        self.assertEqual(upper.source_input_hash, "a" * 64)
        self.assertEqual(upper_provenance.input_hash, "a" * 64)
        self.assertEqual(
            build_draft_feature_snapshot(_target(), (lower,)),
            build_draft_feature_snapshot(_target(), (upper,)),
        )


class DraftFeatureSemanticsTests(unittest.TestCase):
    def test_low_confidence_expected_roles_only_disable_role_dependent_feature(self) -> None:
        unknown_radiant = _team(10, 1, 101, position_confidence=0.0)
        unknown_dire = _team(20, 6, 201, position_confidence=0.0)
        history = [_map(index) for index in range(1, 7)]

        snapshot = build_draft_feature_snapshot(
            _target(radiant=unknown_radiant, dire=unknown_dire), history
        )

        role_fit = snapshot.feature("role_fit_win_rate_diff")
        self.assertIsNone(role_fit.value)
        self.assertEqual(role_fit.coverage, 0.0)
        self.assertEqual(
            role_fit.missing_reason,
            "high_confidence_expected_positions_unavailable",
        )
        self.assertIsNotNone(snapshot.feature("hero_win_rate_diff").value)
        self.assertIsNotNone(snapshot.feature("synergy_win_rate_diff").value)
        self.assertIsNotNone(snapshot.feature("counter_win_rate_edge").value)

    def test_role_fit_uses_historical_observed_position_for_later_target(self) -> None:
        history = [_map(index, radiant_win=True) for index in range(1, 7)]

        matching = build_draft_feature_snapshot(_target(), history)
        swapped = replace(
            history[0],
            radiant_hero_evidence=tuple(
                replace(
                    row,
                    observed_position=(row.observed_position % 5) + 1  # type: ignore[operator]
                )
                for row in history[0].radiant_hero_evidence
            ),
        )
        changed = build_draft_feature_snapshot(_target(), [swapped, *history[1:]])

        self.assertGreater(matching.feature("role_fit_win_rate_diff").support, 0)
        self.assertNotEqual(matching.input_hash, changed.input_hash)
        self.assertLess(
            changed.feature("role_fit_win_rate_diff").support,
            matching.feature("role_fit_win_rate_diff").support,
        )

    def test_observed_role_allows_known_sources_but_rejects_wrong_purpose(self) -> None:
        row = _map(1)
        first = row.radiant_hero_evidence[0]
        for source in (
            RoleSource.AUDITED_ROSTER,
            RoleSource.HISTORICAL_PATTERN,
            RoleSource.SINGLE_MAP,
        ):
            changed = replace(first, observed_role_source=source)
            replace(
                row,
                radiant_hero_evidence=(changed, *row.radiant_hero_evidence[1:]),
            )
        with self.assertRaisesRegex(ValueError, "known role source"):
            replace(first, observed_role_source=RoleSource.UNKNOWN)
        with self.assertRaisesRegex(ValueError, "observed_position purpose"):
            replace(first, observed_role_purpose=RolePurpose.EXPECTED_POSITION)

    def test_counter_counts_each_match_once_per_target_pair(self) -> None:
        snapshot = build_draft_feature_snapshot(_target(), (_map(1),))
        counter = snapshot.feature("counter_win_rate_edge")

        self.assertEqual(counter.support, 25)
        self.assertEqual(counter.coverage, 0.2)
        self.assertAlmostEqual(counter.value, 1.0 / 3.0, places=8)
        self.assertEqual(counter.evidence_ids, ("evidence-1",))

    def test_unsupported_mobility_and_damage_are_null_not_guessed(self) -> None:
        snapshot = build_draft_feature_snapshot(_target(), [_map(1)])

        mobility = snapshot.feature("mobility_global_split_diff")
        damage = snapshot.feature("damage_profile_diff")
        self.assertIsNone(mobility.value)
        self.assertIsNone(damage.value)
        self.assertEqual(mobility.coverage, 0.0)
        self.assertIn("unavailable", mobility.missing_reason or "")
        self.assertIn("unavailable", damage.missing_reason or "")
        self.assertLess(snapshot.pure_coverage, 1.0)

    def test_missing_exact_metrics_lower_coverage_without_zero_substitution(self) -> None:
        snapshot = build_draft_feature_snapshot(
            _target(), [_map(1, include_metrics=False)]
        )

        for name in (
            "control_initiation_proxy_diff",
            "save_sustain_proxy_diff",
            "wave_clear_proxy_diff",
            "farm_demand_balance_diff",
        ):
            feature = snapshot.feature(name)
            self.assertIsNone(feature.value, name)
            self.assertEqual(feature.coverage, 0.0, name)

    def test_context_uses_explicit_style_snapshots_and_available_player_scores(self) -> None:
        rows = [_map(index, radiant_win=True) for index in range(1, 7)]

        snapshot = build_draft_feature_snapshot(_target(), rows)

        self.assertGreater(snapshot.feature("context_comeback_rate_diff").value, 0)
        self.assertGreater(snapshot.feature("context_throw_resilience_diff").value, 0)
        self.assertGreater(snapshot.feature("context_closeout_rate_diff").value, 0)
        self.assertGreater(snapshot.feature("context_player_form_diff").value, 0)
        self.assertEqual(
            snapshot.feature("context_comeback_rate_diff").evidence_ids,
            tuple(
                sorted(
                    (
                        snapshot_target.provenance.input_hash
                        for snapshot_target in (
                            _target().radiant_style,
                            _target().dire_style,
                        )
                        if snapshot_target is not None
                    )
                )
            ),
        )

    def test_each_style_rate_exposes_its_own_support_and_coverage(self) -> None:
        radiant_style = replace(
            _style(10, mode=AvailabilityMode.PROSPECTIVE, radiant=True),
            comeback_rate=DraftStyleRateSnapshot(0.7, 2, 0.4),
            throw_resilience_rate=DraftStyleRateSnapshot(0.8, 7, 1.0),
            closeout_rate=DraftStyleRateSnapshot(0.75, 11, 0.8),
        )
        dire_style = replace(
            _style(20, mode=AvailabilityMode.PROSPECTIVE, radiant=False),
            comeback_rate=DraftStyleRateSnapshot(0.3, 3, 0.6),
            throw_resilience_rate=DraftStyleRateSnapshot(0.4, 5, 0.7),
            closeout_rate=DraftStyleRateSnapshot(0.35, 13, 0.9),
        )

        snapshot = build_draft_feature_snapshot(
            _target(radiant_style=radiant_style, dire_style=dire_style),
            (_map(1),),
        )

        comeback = snapshot.feature("context_comeback_rate_diff")
        throw = snapshot.feature("context_throw_resilience_diff")
        closeout = snapshot.feature("context_closeout_rate_diff")
        self.assertEqual((comeback.support, comeback.coverage), (5, 0.4))
        self.assertEqual((throw.support, throw.coverage), (12, 0.7))
        self.assertEqual((closeout.support, closeout.coverage), (24, 0.8))

        with self.assertRaisesRegex(ValueError, "zero support and coverage"):
            DraftStyleRateSnapshot(None, 1, 0.0)

    def test_series_signal_uses_only_completed_earlier_maps(self) -> None:
        earlier = _map(1, radiant_win=True, series_id=700)
        other_series = _map(2, radiant_win=False, series_id=701)
        future_same_series = _map(
            3,
            radiant_win=False,
            series_id=700,
            completed_at=CUTOFF + timedelta(seconds=1),
            first_usable_at=CUTOFF + timedelta(minutes=1),
        )

        snapshot = build_draft_feature_snapshot(
            _target(), [future_same_series, other_series, earlier]
        )
        series = snapshot.feature("context_series_prior_win_diff")

        self.assertGreater(series.value, 0)
        self.assertEqual(series.evidence_ids, ("evidence-1",))

    def test_feature_schema_and_evidence_are_stable_and_auditable(self) -> None:
        snapshot = build_draft_feature_snapshot(_target(), [_map(2), _map(1)])

        self.assertEqual(snapshot.feature_schema, FEATURE_SCHEMA)
        self.assertEqual(snapshot.feature_schema_hash, FEATURE_SCHEMA_HASH)
        self.assertEqual(len(snapshot.feature_schema_hash), 64)
        self.assertEqual(snapshot.evidence_ids, ("evidence-2", "evidence-1"))
        self.assertEqual(snapshot.support, 2)
        self.assertEqual(
            set(snapshot.feature_schema),
            {row.name for row in (*snapshot.pure_features, *snapshot.context_features)},
        )

    def test_availability_cannot_precede_completion(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot precede"):
            _map(
                1,
                completed_at=CUTOFF - timedelta(hours=1),
                first_usable_at=CUTOFF - timedelta(hours=2),
            )


if __name__ == "__main__":
    unittest.main()
