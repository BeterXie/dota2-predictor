from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from web import app as web_app
from web import main as web_main
from web import queries
from web.schemas import PrematchRequest


def test_web_database_sources_have_one_explicit_precedence(tmp_path: Path) -> None:
    config_path = tmp_path / "web" / "config.yaml"
    config_path.parent.mkdir()
    config = {"database": "config.db"}
    environment = {"DATABASE_PATH": str(tmp_path / "environment.db")}

    selected, source = web_main.resolve_database_path(
        tmp_path / "cli.db",
        config,
        config_path,
        environment,
    )
    assert selected == (tmp_path / "cli.db").resolve()
    assert source == "cli"

    selected, source = web_main.resolve_database_path(
        None,
        config,
        config_path,
        environment,
    )
    assert selected == (tmp_path / "environment.db").resolve()
    assert source == "environment"

    selected, source = web_main.resolve_database_path(
        None,
        config,
        config_path,
        {},
    )
    assert selected == (config_path.parent / "config.db").resolve()
    assert source == "config"


def test_web_main_hands_the_resolved_path_to_queries_and_reload_children(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "candidate.db"
    previous = queries.DB_PATH
    invoked: dict[str, object] = {}
    monkeypatch.setattr(
        web_main.uvicorn,
        "run",
        lambda application, **kwargs: invoked.update(
            {"application": application, **kwargs}
        ),
    )
    try:
        web_main.main(
            [
                "--database",
                str(database),
                "--config",
                str(tmp_path / "missing.yaml"),
            ]
        )
        assert queries.DB_PATH == str(database.resolve())
        assert web_main.os.environ["DATABASE_PATH"] == str(database.resolve())
        assert invoked["application"] == "web.app:app"
    finally:
        queries.init_db(previous)


def test_prediction_code_uses_the_queries_database_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "candidate.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE teams (team_id INTEGER PRIMARY KEY)")
    connection.executemany("INSERT INTO teams VALUES (?)", [(10,), (20,)])
    connection.commit()
    connection.close()

    observed: list[str] = []

    def predict_match(database_path: str, *_: object, **__: object) -> dict:
        observed.append(database_path)
        return {
            "radiant_win_prob": 0.5,
            "confidence": "low",
            "confidence_score": 0.1,
            "components": {"hero_matchup": {}},
            "weights_used": {},
            "raw_score": 0.0,
        }

    def format_output(*args: object) -> dict[str, object]:
        observed.append(str(args[-1]))
        return {"status": "ok"}

    output = SimpleNamespace(
        format_output=format_output,
        save_prediction=lambda *_: "prediction.json",
        _sanitize=lambda value: value,
    )
    previous = queries.DB_PATH
    queries.init_db(str(database))
    monkeypatch.setattr(
        web_app,
        "_get_prematch_builder",
        lambda: (predict_match, output),
    )
    monkeypatch.setattr(web_app, "_PREDICTIONS_DIR", str(tmp_path / "predictions"))
    request = PrematchRequest(
        radiant_id=10,
        dire_id=20,
        radiant_heroes=[1, 2, 3, 4, 5],
        dire_heroes=[6, 7, 8, 9, 10],
    )
    try:
        result = web_app.create_prematch_prediction(request)
    finally:
        queries.init_db(previous)

    assert result["status"] == "ok"
    assert observed == [str(database.resolve()), str(database.resolve())]
