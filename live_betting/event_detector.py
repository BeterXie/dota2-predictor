"""Derive monotonic Dota 2 events from consecutive provider frames."""

from __future__ import annotations

from .models import LiveEvent, LiveFrame


def detect_events(previous: LiveFrame | None, current: LiveFrame) -> list[LiveEvent]:
    if previous is None or previous.provider_game_id != current.provider_game_id:
        return []
    if (previous.game_time is not None and current.game_time is not None and
            current.game_time < previous.game_time):
        raise ValueError("game time regressed")
    events: list[LiveEvent] = []
    for side, old, new in (
        ("team_one", previous.team_one_kills, current.team_one_kills),
        ("team_two", previous.team_two_kills, current.team_two_kills),
    ):
        if old is None or new is None:
            continue
        if new < old:
            raise ValueError(f"kill score regressed for {side}")
        for score in range(old + 1, new + 1):
            event_type = "first_blood" if score == 1 and (
                (current.team_two_kills if side == "team_one" else current.team_one_kills) == 0
            ) else "kill"
            event_id = f"{current.provider_game_id}:{side}:kill:{score}"
            events.append(
                LiveEvent(
                    provider=current.provider,
                    provider_event_id=event_id,
                    provider_match_id=current.provider_match_id,
                    provider_game_id=current.provider_game_id,
                    event_type=event_type,
                    source_at=current.source_at,
                    received_at=current.received_at,
                    game_time=current.game_time,
                    team=side,
                    value=float(score),
                    raw={"derived_from_sequence": current.sequence},
                )
            )
            if score in {5, 10, 15}:
                events.append(
                    LiveEvent(
                        provider=current.provider,
                        provider_event_id=f"{current.provider_game_id}:race:{score}",
                        provider_match_id=current.provider_match_id,
                        provider_game_id=current.provider_game_id,
                        event_type=f"first_to_{score}_kills",
                        source_at=current.source_at,
                        received_at=current.received_at,
                        game_time=current.game_time,
                        team=side,
                        value=float(score),
                        raw={"derived_from_sequence": current.sequence},
                    )
                )
    return events
