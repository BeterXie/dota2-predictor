from pathlib import Path

import pytest

from scripts.relocate_raw_source_root import _destination, _materialize


def test_relocation_preserves_content_addressed_relative_path(tmp_path: Path) -> None:
    source_root = tmp_path / "old" / "raw-sources"
    source = source_root / "opendota" / "ab" / "abcdef.json.gz"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"immutable")
    target_root = tmp_path / "current" / "raw-sources"

    destination = _destination(source.resolve(), (source_root.resolve(),), target_root)

    assert destination == (
        target_root / "opendota" / "ab" / "abcdef.json.gz"
    ).resolve()
    assert _materialize(source, destination) is True
    assert destination.read_bytes() == b"immutable"
    assert _materialize(source, destination) is False


def test_relocation_rejects_artifact_outside_declared_roots(tmp_path: Path) -> None:
    source = tmp_path / "outside" / "artifact.json.gz"
    source.parent.mkdir()
    source.write_bytes(b"immutable")

    with pytest.raises(ValueError, match="outside the declared source roots"):
        _destination(
            source.resolve(),
            ((tmp_path / "old" / "raw-sources").resolve(),),
            tmp_path / "current" / "raw-sources",
        )
