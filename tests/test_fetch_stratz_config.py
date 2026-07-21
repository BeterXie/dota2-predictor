from fetch.fetch_stratz_matchups import resolve_stratz_token


def test_stratz_api_token_takes_priority_over_legacy_name() -> None:
    environment = {
        "STRATZ_API_TOKEN": "primary-token",
        "STRATZ_TOKEN": "legacy-token",
    }

    assert resolve_stratz_token(environment) == "primary-token"


def test_stratz_token_remains_a_legacy_fallback() -> None:
    assert resolve_stratz_token({"STRATZ_TOKEN": "legacy-token"}) == "legacy-token"


def test_stratz_token_is_missing_when_both_names_are_empty() -> None:
    environment = {"STRATZ_API_TOKEN": "  ", "STRATZ_TOKEN": ""}

    assert resolve_stratz_token(environment) is None
