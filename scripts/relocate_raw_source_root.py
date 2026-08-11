"""Relocate immutable raw-source artifacts into one stable data root."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import build_engine, require_database_url  # noqa: E402
from database.session import PostgresSession  # noqa: E402
from event_intelligence.raw_registry import (  # noqa: E402
    relocate_raw_source_artifacts,
)


def _destination(source: Path, source_roots: tuple[Path, ...], target_root: Path) -> Path:
    for source_root in source_roots:
        try:
            relative = source.relative_to(source_root)
        except ValueError:
            continue
        return (target_root / relative).resolve()
    raise ValueError(f"artifact is outside the declared source roots: {source}")


def _materialize(source: Path, destination: Path) -> bool:
    if destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        temporary = destination.with_suffix(destination.suffix + ".part")
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
    return True


def relocate(
    database_url: str,
    *,
    source_roots: tuple[Path, ...],
    target_root: Path,
    batch_size: int,
    reason: str,
    actor: str,
) -> dict[str, int | str]:
    sources = tuple(root.resolve() for root in source_roots)
    target = target_root.resolve()
    if not sources or target in sources:
        raise ValueError("source and target roots must be distinct")
    connection = PostgresSession(build_engine(database_url))
    try:
        rows = connection.execute(
            "SELECT artifact_id, storage_path FROM raw_source_artifacts "
            "ORDER BY artifact_id"
        ).fetchall()
        connection.rollback()
        planned: list[tuple[str, Path, Path]] = []
        for row in rows:
            artifact_id = str(row[0])
            source = Path(str(row[1])).resolve()
            if source.is_relative_to(target):
                continue
            if not source.is_file():
                raise RuntimeError(f"registered source artifact is missing: {source}")
            planned.append((artifact_id, source, _destination(source, sources, target)))

        linked = 0
        relocated = 0
        for offset in range(0, len(planned), batch_size):
            batch = planned[offset : offset + batch_size]
            replacements: dict[str, Path] = {}
            for artifact_id, source, destination in batch:
                linked += int(_materialize(source, destination))
                replacements[artifact_id] = destination
            relocation_ids = relocate_raw_source_artifacts(
                connection,
                replacements,
                allowed_new_roots=(target,),
                incoming_files=replacements,
                allowed_incoming_roots=(target,),
                reason=reason,
                actor=actor,
                relocated_at=datetime.now(timezone.utc),
            )
            relocated += len(relocation_ids)
            print(
                json.dumps(
                    {
                        "planned": len(planned),
                        "processed": min(offset + len(batch), len(planned)),
                        "relocated": relocated,
                    }
                ),
                flush=True,
            )
        return {
            "planned": len(planned),
            "linked": linked,
            "relocated": relocated,
            "target_root": str(target),
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--source-root", action="append", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--reason", default="unified_master_raw_source_root")
    parser.add_argument("--actor", default="operator")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 500:
        parser.error("--batch-size must be between 1 and 500")
    database_url = require_database_url(args.database_url)
    result = relocate(
        database_url,
        source_roots=tuple(args.source_root),
        target_root=args.target_root,
        batch_size=args.batch_size,
        reason=args.reason,
        actor=args.actor,
    )
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
