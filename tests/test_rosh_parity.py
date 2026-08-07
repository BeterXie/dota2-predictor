from __future__ import annotations

import gzip

import pytest

from live_betting.rosh_parity import ArtifactError, ExactByteArtifactStore


def test_exact_byte_artifact_is_content_addressed_and_idempotent(tmp_path) -> None:
    store = ExactByteArtifactStore(tmp_path)
    body = b'{"data":{"value":1}}'

    first = store.persist(body)
    second = store.persist(body)

    assert first == second
    path = tmp_path.joinpath(*first.relative_path.split("/"))
    assert gzip.decompress(path.read_bytes()) == body


def test_exact_byte_artifact_rejects_credentials(tmp_path) -> None:
    store = ExactByteArtifactStore(tmp_path)

    with pytest.raises(ArtifactError, match="credential"):
        store.persist(b'{"Authorization":"Bearer secret"}')
