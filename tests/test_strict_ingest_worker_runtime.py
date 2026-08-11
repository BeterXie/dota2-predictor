import asyncio
from types import SimpleNamespace

from scripts import run_strict_event_ingest


def test_long_strict_ingest_cycle_keeps_health_heartbeat_fresh(monkeypatch) -> None:
    heartbeats: list[str] = []
    runtime = SimpleNamespace()

    async def scenario() -> str:
        two_heartbeats = asyncio.Event()

        def record(_runtime, status, _at) -> None:
            heartbeats.append(status)
            if len(heartbeats) >= 2:
                two_heartbeats.set()

        monkeypatch.setattr(
            run_strict_event_ingest,
            "_record_runtime_health",
            record,
        )

        async def operation() -> str:
            await asyncio.wait_for(two_heartbeats.wait(), timeout=0.5)
            return "complete"

        return await run_strict_event_ingest._await_with_health_heartbeat(
            runtime,
            operation(),
            interval=0.01,
        )

    result = asyncio.run(scenario())

    assert result == "complete"
    assert len(heartbeats) >= 2
    assert set(heartbeats) == {"starting"}
