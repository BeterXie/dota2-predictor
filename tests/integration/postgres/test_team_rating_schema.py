from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError


TEAM_RATING_TABLES = {
    "team_rating_runs",
    "team_rating_predictions",
    "team_rating_state_snapshots",
}
TEAM_RATING_TRIGGERS = {
    "team_rating_runs_append_only",
    "team_rating_predictions_append_only",
    "team_rating_predictions_cutoff_guard",
    "team_rating_state_snapshots_append_only",
    "team_rating_state_snapshots_cutoff_guard",
}
ROOT = Path(__file__).resolve().parents[3]


def _column_names(engine: Engine, table: str) -> tuple[str, ...]:
    return tuple(column["name"] for column in inspect(engine).get_columns(table))


def _seed_match_authority(
    connection: Connection,
    match_ids: Iterable[int],
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO event_registry (
                event_id, canonical_name, tier, prize_pool_usd,
                main_event_start_at, main_event_end_at, opendota_league_id,
                official_evidence_urls_json, evidence_status,
                scope_policy_version, scope, approval_status,
                approved_by, approved_at, reconciliation_status,
                included_stages_json, excluded_categories_json,
                created_at, updated_at
            ) VALUES (
                'team-rating-event', 'Team Rating Event', 'tier_1', 1000000,
                '2026-01-01T00:00:00Z', '2026-01-10T00:00:00Z', 99001,
                '[]', 'manually_audited', 'scope-v1',
                'formal_main_event', 'approved', 'tester',
                '2025-12-01T00:00:00Z', 'not_required', '[]', '[]',
                '2025-12-01T00:00:00Z', '2025-12-01T00:00:00Z'
            )
            """
        )
    )
    for match_id in match_ids:
        connection.execute(
            text(
                """
                INSERT INTO matches (
                    match_id, radiant_team_id, dire_team_id, radiant_win,
                    duration, start_time, series_id
                ) VALUES (
                    :match_id, 10, 20, true, 2400, 1767225600, 100
                )
                """
            ),
            {"match_id": match_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO match_ingest_status (
                    match_id, event_id, start_time, series_id, map_number,
                    discovered_at, updated_at
                ) VALUES (
                    :match_id, 'team-rating-event', 1767225600,
                    100, :map_number,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                )
                """
            ),
            {"match_id": match_id, "map_number": match_id},
        )


def _insert_rows(connection: Connection) -> None:
    _seed_match_authority(connection, (1,))
    _insert_run(connection)


def _insert_parent_run(
    connection: Connection,
    *,
    run_id: str = "a" * 64,
    availability_mode: str = "reconstructed_walk_forward",
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO team_rating_runs (
                run_id, rating_version, artifact_version, availability_mode,
                training_cutoff, configuration_json, training_input_hash,
                metrics_json, status, created_at
            ) VALUES (
                :run_id, 'team-rating-elo-v1', 'team-rating-artifact-v1',
                :availability_mode, '2026-01-01T00:00:00Z',
                :configuration_json, :training_hash, :metrics_json,
                'trained',
                '2026-08-05T00:00:00Z'
            )
            """
        ),
        {
            "run_id": run_id,
            "availability_mode": availability_mode,
            "configuration_json": (
                '{"artifact_hash":"artifact","config":{"scale":400.0}}'
            ),
            "training_hash": "b" * 64,
            "metrics_json": (
                '{"parameter_selection":{"support":1},'
                '"radiant_prior_probability":0.5}'
            ),
        },
    )


def _insert_run(connection: Connection) -> None:
    _insert_parent_run(connection)
    connection.execute(
        text(
            """
            INSERT INTO team_rating_predictions (
                run_id, match_id, prediction_cutoff, cutoff_source,
                radiant_team_id, dire_team_id, radiant_rating, dire_rating,
                rating_diff, raw_probability, radiant_roster_continuity,
                dire_roster_continuity, support, input_hash,
                eventual_radiant_win, status, created_at
            ) VALUES (
                :run_id, 1, '2026-01-01T00:00:00Z',
                'reconstructed_map_start', 10, 20, 1510.0, 1490.0,
                20.0, 0.55, 1.0, 0.8, 12, :input_hash, 1, 'settled',
                '2026-08-05T00:00:00Z'
            )
            """
        ),
        {"run_id": "a" * 64, "input_hash": "c" * 64},
    )
    connection.execute(
        text(
            """
            INSERT INTO team_rating_state_snapshots (
                snapshot_key, run_id, as_of, team_id, rating, maps_seen,
                roster_json, last_observed_at, state_hash, created_at
            ) VALUES (
                :snapshot_key, :run_id, '2026-01-01T00:00:00Z', 10,
                1510.0, 12, '[1,2,3,4,5]', '2025-12-31T00:00:00Z',
                :state_hash, '2026-08-05T00:00:00Z'
            )
            """
        ),
        {
            "snapshot_key": "d" * 64,
            "run_id": "a" * 64,
            "state_hash": "e" * 64,
        },
    )


def test_team_rating_schema_has_exact_tables_constraints_and_indexes(
    postgres_engine: Engine,
) -> None:
    inspector = inspect(postgres_engine)
    assert TEAM_RATING_TABLES <= set(inspector.get_table_names())
    assert _column_names(postgres_engine, "team_rating_runs") == (
        "run_id",
        "rating_version",
        "artifact_version",
        "availability_mode",
        "training_cutoff",
        "configuration_json",
        "training_input_hash",
        "metrics_json",
        "status",
        "created_at",
    )
    assert _column_names(postgres_engine, "team_rating_predictions") == (
        "prediction_id",
        "run_id",
        "match_id",
        "prediction_cutoff",
        "cutoff_source",
        "radiant_team_id",
        "dire_team_id",
        "radiant_rating",
        "dire_rating",
        "rating_diff",
        "raw_probability",
        "radiant_roster_continuity",
        "dire_roster_continuity",
        "support",
        "input_hash",
        "eventual_radiant_win",
        "status",
        "created_at",
    )
    assert _column_names(postgres_engine, "team_rating_state_snapshots") == (
        "snapshot_key",
        "run_id",
        "as_of",
        "team_id",
        "rating",
        "maps_seen",
        "roster_json",
        "last_observed_at",
        "state_hash",
        "created_at",
    )

    prediction_fks = {
        (tuple(row["constrained_columns"]), row["referred_table"])
        for row in inspector.get_foreign_keys("team_rating_predictions")
    }
    snapshot_fks = {
        (tuple(row["constrained_columns"]), row["referred_table"])
        for row in inspector.get_foreign_keys("team_rating_state_snapshots")
    }
    assert (("run_id",), "team_rating_runs") in prediction_fks
    assert (("match_id",), "match_ingest_status") in prediction_fks
    assert snapshot_fks == {(('run_id',), "team_rating_runs")}

    indexes = {
        row["name"]
        for table in TEAM_RATING_TABLES
        for row in inspector.get_indexes(table)
    }
    assert {
        "idx_team_rating_runs_mode_cutoff",
        "idx_team_rating_predictions_match_cutoff",
        "idx_team_rating_predictions_run",
        "idx_team_rating_state_team_as_of",
    } <= indexes
    with postgres_engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        triggers = set(
            connection.execute(
                text(
                    "SELECT trigger_name FROM information_schema.triggers "
                    "WHERE trigger_schema=current_schema()"
                )
            ).scalars()
        )
    assert revision == "20260806_0030"
    assert TEAM_RATING_TRIGGERS <= triggers


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("availability_mode", "mixed"),
        ("training_input_hash", "not-a-hash"),
        ("training_cutoff", "2026-01-01 00:00:00"),
        ("configuration_json", "[]"),
        ("configuration_json", '{"b":1,"a":2}'),
        ("metrics_json", '{ "support":1}'),
    ),
)
def test_team_rating_run_checks_fail_closed(
    postgres_engine: Engine,
    column: str,
    value: str,
) -> None:
    statement = text(
        """
        INSERT INTO team_rating_runs (
            run_id, rating_version, artifact_version, availability_mode,
            training_cutoff, configuration_json, training_input_hash,
            metrics_json, status, created_at
        ) VALUES (
            :run_id, :rating_version, :artifact_version,
            :availability_mode, :training_cutoff,
            :configuration_json, :training_input_hash,
            :metrics_json, :status, :created_at
        )
        """
    )
    parameters = {
        "run_id": "a" * 64,
        "rating_version": "team-rating-elo-v1",
        "artifact_version": "team-rating-artifact-v1",
        "availability_mode": "reconstructed_walk_forward",
        "training_cutoff": "2026-01-01T00:00:00Z",
        "configuration_json": "{}",
        "training_input_hash": "b" * 64,
        "metrics_json": "{}",
        "status": "trained",
        "created_at": "2026-08-05T00:00:00Z",
    }
    parameters[column] = value
    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(statement, parameters)


def test_team_rating_tables_are_append_only(postgres_engine: Engine) -> None:
    with postgres_engine.begin() as connection:
        _insert_rows(connection)

    mutations = (
        "UPDATE team_rating_runs SET status='failed' WHERE run_id=:run_id",
        "DELETE FROM team_rating_predictions WHERE run_id=:run_id",
        "UPDATE team_rating_state_snapshots SET maps_seen=13 WHERE run_id=:run_id",
    )
    for statement in mutations:
        with pytest.raises(DBAPIError, match="append-only"):
            with postgres_engine.begin() as connection:
                connection.execute(text(statement), {"run_id": "a" * 64})


def test_prediction_state_and_cross_table_cutoff_guards_fail_closed(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        _seed_match_authority(connection, (1,))
        _insert_run(connection)

    prediction_sql = text(
        """
        INSERT INTO team_rating_predictions (
            run_id, match_id, prediction_cutoff, cutoff_source,
            radiant_team_id, dire_team_id, radiant_rating, dire_rating,
            rating_diff, raw_probability, radiant_roster_continuity,
            dire_roster_continuity, support, input_hash,
            eventual_radiant_win, status, created_at
        ) VALUES (
            :run_id, :match_id, :prediction_cutoff, :cutoff_source,
            :radiant_team_id, :dire_team_id,
            1510.0, 1490.0, 20.0, :raw_probability,
            1.0, 0.8, 12, :input_hash, :eventual_radiant_win,
            :status, '2026-08-05T00:00:00Z'
        )
        """
    )
    base = {
        "run_id": "a" * 64,
        "match_id": 1,
        "prediction_cutoff": "2026-01-01T00:00:00Z",
        "cutoff_source": "reconstructed_map_start",
        "radiant_team_id": 10,
        "dire_team_id": 20,
        "raw_probability": 0.55,
        "input_hash": "c" * 64,
        "eventual_radiant_win": 1,
        "status": "settled",
    }
    attacks = (
        {**base, "eventual_radiant_win": None},
        {**base, "status": "predicted", "eventual_radiant_win": None, "raw_probability": None},
        {
            **base,
            "status": "insufficient_evidence",
            "eventual_radiant_win": None,
        },
        {**base, "prediction_cutoff": "2025-12-31T23:59:59Z"},
        {**base, "prediction_cutoff": "2026-01-01T00:00:01Z"},
        {**base, "radiant_team_id": 20, "dire_team_id": 10},
        {**base, "eventual_radiant_win": 0},
        {**base, "cutoff_source": "prospective_archive"},
    )
    for parameters in attacks:
        with pytest.raises(DBAPIError):
            with postgres_engine.begin() as connection:
                connection.execute(prediction_sql, parameters)

    with pytest.raises(DBAPIError, match="must equal training cutoff"):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO team_rating_state_snapshots (
                        snapshot_key, run_id, as_of, team_id, rating,
                        maps_seen, roster_json, last_observed_at,
                        state_hash, created_at
                    ) VALUES (
                        :snapshot_key, :run_id, '2026-01-01T00:00:01Z',
                        10, 1510.0, 12, '[1,2,3,4,5]',
                        '2025-12-31T00:00:00Z', :state_hash,
                        '2026-08-05T00:00:00Z'
                    )
                    """
                ),
                {
                    "snapshot_key": "d" * 64,
                    "run_id": "a" * 64,
                    "state_hash": "e" * 64,
                },
            )

    with postgres_engine.begin() as connection:
        for match_id in (2, 3, 4):
            connection.execute(
                text(
                    """
                    INSERT INTO match_ingest_status (
                        match_id, event_id, discovered_at, updated_at
                    ) VALUES (
                        :match_id, 'team-rating-event',
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                    )
                    """
                ),
                {"match_id": match_id},
            )
        connection.execute(
            text(
                """
                INSERT INTO matches (
                    match_id, radiant_team_id, dire_team_id, radiant_win,
                    duration, start_time, series_id
                ) VALUES (3, 10, 20, true, 2400, 0, 100)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO matches (
                    match_id, radiant_win, duration, start_time, series_id
                ) VALUES (4, true, 2400, 1767225600, 100)
                """
            )
        )
    for match_id in (2, 3):
        with pytest.raises(DBAPIError, match="target map start is unavailable"):
            with postgres_engine.begin() as connection:
                connection.execute(
                    prediction_sql,
                    {
                        **base,
                        "match_id": match_id,
                        "input_hash": f"{match_id:064x}",
                    },
                )

    with pytest.raises(DBAPIError, match="target team authority is unavailable"):
        with postgres_engine.begin() as connection:
            connection.execute(
                prediction_sql,
                {**base, "match_id": 4, "input_hash": f"{4:064x}"},
            )


def test_prediction_mode_and_missing_result_authority_fail_closed(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        _seed_match_authority(connection, (1,))
        _insert_parent_run(
            connection,
            run_id="b" * 64,
            availability_mode="prospective",
        )
        _insert_parent_run(connection, run_id="c" * 64)

    statement = text(
        """
        INSERT INTO team_rating_predictions (
            run_id, match_id, prediction_cutoff, cutoff_source,
            radiant_team_id, dire_team_id, radiant_rating, dire_rating,
            rating_diff, raw_probability, radiant_roster_continuity,
            dire_roster_continuity, support, input_hash,
            eventual_radiant_win, status, created_at
        ) VALUES (
            :run_id, 1, '2026-01-01T00:00:00Z', :cutoff_source,
            10, 20, 1510.0, 1490.0, 20.0, 0.55, 1.0, 0.8, 12,
            :input_hash, :eventual_radiant_win, :status,
            '2026-08-05T00:00:00Z'
        )
        """
    )
    with pytest.raises(DBAPIError, match="prospective cutoff source is invalid"):
        with postgres_engine.begin() as connection:
            connection.execute(
                statement,
                {
                    "run_id": "b" * 64,
                    "cutoff_source": "reconstructed_map_start",
                    "input_hash": "d" * 64,
                    "eventual_radiant_win": None,
                    "status": "predicted",
                },
            )

    with postgres_engine.begin() as connection:
        connection.execute(
            text("UPDATE matches SET radiant_win=NULL WHERE match_id=1")
        )
        connection.execute(
            statement,
            {
                "run_id": "b" * 64,
                "cutoff_source": "prospective_archive",
                "input_hash": "d" * 64,
                "eventual_radiant_win": None,
                "status": "predicted",
            },
        )

    with pytest.raises(DBAPIError, match="target result authority disagrees"):
        with postgres_engine.begin() as connection:
            connection.execute(
                statement,
                {
                    "run_id": "c" * 64,
                    "cutoff_source": "reconstructed_map_start",
                    "input_hash": "e" * 64,
                    "eventual_radiant_win": 1,
                    "status": "settled",
                },
            )


@pytest.mark.parametrize(
    "roster_json",
    (
        "[1, 2, 3, 4, 5]",
        "[1,1,2,3,4]",
        "[0,1,2,3,4]",
        '["1","2","3","4","5"]',
    ),
)
def test_team_rating_snapshot_roster_checks_fail_closed(
    postgres_engine: Engine,
    roster_json: str,
) -> None:
    with postgres_engine.begin() as connection:
        _insert_parent_run(connection)

    with pytest.raises(DBAPIError):
        with postgres_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO team_rating_state_snapshots (
                        snapshot_key, run_id, as_of, team_id, rating,
                        maps_seen, roster_json, last_observed_at,
                        state_hash, created_at
                    ) VALUES (
                        :snapshot_key, :run_id, '2026-01-01T00:00:00Z',
                        10, 1510.0, 12, :roster_json,
                        '2025-12-31T00:00:00Z', :state_hash,
                        '2026-08-05T00:00:00Z'
                    )
                    """
                ),
                {
                    "snapshot_key": "d" * 64,
                    "run_id": "a" * 64,
                    "roster_json": roster_json,
                    "state_hash": "e" * 64,
                },
            )


def test_team_rating_migration_downgrades_and_reupgrades(
    postgres_engine: Engine,
) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        postgres_engine.url.render_as_string(hide_password=False),
    )
    postgres_engine.dispose()

    command.downgrade(config, "20260802_0021")
    assert not (TEAM_RATING_TABLES & set(inspect(postgres_engine).get_table_names()))
    with postgres_engine.connect() as connection:
        functions = set(
            connection.execute(
                text(
                    "SELECT proname FROM pg_proc "
                    "WHERE proname LIKE 'team_rating_%'"
                )
            ).scalars()
        )
    assert "team_rating_canonical_json" not in functions
    assert "team_rating_roster_is_valid" not in functions

    command.upgrade(config, "head")
    assert TEAM_RATING_TABLES <= set(inspect(postgres_engine).get_table_names())
    with postgres_engine.begin() as connection:
        _insert_rows(connection)
