from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database.session import PostgresSession


def test_postgres_session_supports_named_rows_and_nested_transactions(
    postgres_engine,
) -> None:
    session = PostgresSession(postgres_engine)
    with session.transaction():
        session.execute(
            "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
            (1, "Anti-Mage"),
        )
        with session.transaction():
            row = session.execute(
                "SELECT hero_id, localized_name FROM heroes WHERE hero_id = ?",
                (1,),
            ).fetchone()
            assert row is not None
            assert row[0] == 1
            assert row["localized_name"] == "Anti-Mage"
            assert dict(row) == {"hero_id": 1, "localized_name": "Anti-Mage"}

    with postgres_engine.connect() as connection:
        assert connection.execute(
            text("SELECT localized_name FROM heroes WHERE hero_id = 1")
        ).scalar_one() == "Anti-Mage"


def test_postgres_session_rolls_back_failed_outer_transaction(
    postgres_engine,
) -> None:
    session = PostgresSession(postgres_engine)
    try:
        with session.transaction():
            session.execute(
                "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
                (2, "Axe"),
            )
            raise RuntimeError("rollback")
    except RuntimeError:
        pass

    with postgres_engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM heroes WHERE hero_id = 2")
        ).scalar_one() == 0


def test_postgres_result_consumes_buffered_rows(postgres_engine) -> None:
    session = PostgresSession(postgres_engine)
    with session.transaction():
        session.executemany(
            "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
            [(10, "Morphling"), (11, "Shadow Fiend"), (12, "Phantom Lancer")],
        )

    result = session.execute(
        "SELECT hero_id FROM heroes WHERE hero_id BETWEEN 10 AND 12 ORDER BY hero_id"
    )
    assert result.fetchone()[0] == 10
    assert [row[0] for row in result.fetchall()] == [11, 12]
    assert result.fetchone() is None


def test_postgres_session_supports_explicit_begin_commit_and_rollback(
    postgres_engine,
) -> None:
    session = PostgresSession(postgres_engine)
    session.begin()
    session.execute(
        "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
        (20, "Puck"),
    )
    session.commit()

    session.begin()
    session.execute(
        "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
        (21, "Batrider"),
    )
    session.rollback()

    with postgres_engine.connect() as connection:
        assert connection.execute(
            text("SELECT localized_name FROM heroes WHERE hero_id = 20")
        ).scalar_one() == "Puck"
        assert connection.execute(
            text("SELECT COUNT(*) FROM heroes WHERE hero_id = 21")
        ).scalar_one() == 0


def test_failed_implicit_write_releases_aborted_connection(postgres_engine) -> None:
    session = PostgresSession(postgres_engine)
    session.execute(
        "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
        (30, "Lich"),
    )
    session.commit()

    with pytest.raises(IntegrityError):
        session.execute(
            "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
            (30, "Duplicate Lich"),
        )

    assert not session.in_transaction
    assert session.execute(
        "SELECT COUNT(*) FROM heroes WHERE hero_id = ?",
        (30,),
    ).scalar_one() == 1


def test_failed_implicit_executemany_rolls_back_and_releases_connection(
    postgres_engine,
) -> None:
    session = PostgresSession(postgres_engine)
    session.execute(
        "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
        (31, "Luna"),
    )
    session.commit()

    with pytest.raises(IntegrityError):
        session.executemany(
            "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
            [(32, "Mirana"), (31, "Duplicate Luna"), (33, "Naga Siren")],
        )

    assert not session.in_transaction
    assert session.execute(
        "SELECT COUNT(*) FROM heroes WHERE hero_id IN (?, ?, ?)",
        (31, 32, 33),
    ).scalar_one() == 1


def test_failed_explicit_transaction_is_rolled_back_by_context(
    postgres_engine,
) -> None:
    session = PostgresSession(postgres_engine)
    with pytest.raises(IntegrityError):
        with session.transaction():
            session.execute(
                "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
                (40, "Zeus"),
            )
            session.execute(
                "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
                (40, "Duplicate Zeus"),
            )

    assert session.execute(
        "SELECT COUNT(*) FROM heroes WHERE hero_id = ?",
        (40,),
    ).scalar_one() == 0


def test_failed_nested_transaction_rolls_back_only_savepoint(postgres_engine) -> None:
    session = PostgresSession(postgres_engine)
    with session.transaction():
        session.execute(
            "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
            (50, "Viper"),
        )
        with pytest.raises(IntegrityError):
            with session.transaction():
                session.execute(
                    "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
                    (51, "Venomancer"),
                )
                session.execute(
                    "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
                    (50, "Duplicate Viper"),
                )
        session.execute(
            "INSERT INTO heroes (hero_id, localized_name) VALUES (?, ?)",
            (52, "Riki"),
        )

    with postgres_engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM heroes WHERE hero_id IN (50, 51, 52)")
        ).scalar_one() == 2
