from __future__ import annotations

from sqlalchemy import text

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
