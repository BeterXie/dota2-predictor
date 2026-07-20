from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import pytest
import psutil
import managed_child_bootstrap
import live_betting.service_coordination as service_coordination

from live_betting.service_coordination import (
    MANAGED_CHILD_BOOTSTRAP_SCRIPT,
    ProcessIdentity,
    SingleInstanceLock,
    WriterScanResult,
    add_single_database_argument,
    bind_manager_child_authority,
    database_authority_lock_paths,
    database_global_authority_lock_paths,
    database_offline_authority,
    database_service_authority_lock_paths,
    database_service_lock_path,
    database_web_lock_path,
    database_web_authority_lock_paths,
    database_writer_authority,
    manager_child_authority,
    manager_child_process_environment,
    managed_child_command,
    managed_child_target,
    resolve_process_identity,
    scan_managed_writers,
    terminate_subprocess_tree,
    web_fetch_child_authority,
    web_fetch_process_environment,
)
from live_betting.service_coordination import (
    _validate_manager_child_authority,
    _windows_commands_match,
)


ROOT = Path(__file__).resolve().parents[1]
WRITER_ENTRYPOINTS = (
    "fetch/fetch_matchups.py",
    "fetch/fetch_stratz_matchups.py",
    "fetch/hero_meta.py",
    "fetch/main.py",
    "live_betting/browser_companion.py",
    "live_betting/draft_publisher.py",
    "live_betting/monitor.py",
    "live_betting/postmatch_monitor.py",
    "live_betting/shadow_monitor.py",
    "scripts/accept_strict_live_mapping.py",
    "scripts/assign_strict_event_roles.py",
    "scripts/backfill_early_game.py",
    "scripts/backfill_team_profiles.py",
    "scripts/build_strict_team_profiles.py",
    "scripts/cleanup_vision_evidence.py",
    "scripts/invalidate_vision_observations.py",
    "scripts/refresh_draft_prediction_validations.py",
    "scripts/run_dota_shadow_service.py",
    "scripts/run_notification_worker.py",
    "scripts/run_strict_draft_backtest.py",
    "scripts/run_strict_event_ingest.py",
    "scripts/score_strict_event_players.py",
    "scripts/supervise_raybet_streams.py",
    "scripts/watch_raybet_stream.py",
)


@contextlib.contextmanager
def _held_lock_paths(paths: tuple[Path, ...]):
    with contextlib.ExitStack() as locks:
        for path in paths:
            locks.enter_context(SingleInstanceLock(path))
        yield


def _held_database_authority(database: Path):
    return _held_lock_paths(database_authority_lock_paths(database))


def _held_web_authority(database: Path):
    return _held_lock_paths(database_web_authority_lock_paths(database))


def test_web_and_service_role_authorities_can_coexist(tmp_path: Path) -> None:
    database = tmp_path / "coexist.db"
    database.touch()

    with _held_web_authority(database):
        with _held_lock_paths(database_service_authority_lock_paths(database)):
            for lock_path in database_web_authority_lock_paths(database):
                with pytest.raises(RuntimeError, match="already held|collision"):
                    with SingleInstanceLock(lock_path):
                        pass


@pytest.mark.parametrize("role", ["service", "web"])
def test_offline_authority_is_blocked_by_each_online_role_and_unwinds(
    tmp_path: Path,
    role: str,
) -> None:
    database = tmp_path / f"{role}.db"
    database.touch()
    held_paths = (
        database_service_authority_lock_paths(database)
        if role == "service"
        else database_web_authority_lock_paths(database)
    )

    with _held_lock_paths(held_paths):
        with pytest.raises(RuntimeError, match="already held"):
            with database_offline_authority(
                database,
                writer_scanner=lambda _: WriterScanResult((), ()),
            ):
                pytest.fail("offline authority bypassed an online role")
        if role == "web":
            with _held_lock_paths(database_service_authority_lock_paths(database)):
                pass


def test_offline_authority_uses_canonical_four_lock_order(tmp_path: Path) -> None:
    database = tmp_path / "ordered.db"
    database.touch()
    events: list[tuple[str, Path]] = []

    class RecordingLock:
        def __init__(self, path: Path) -> None:
            self.path = path

        def __enter__(self) -> "RecordingLock":
            events.append(("enter", self.path))
            return self

        def __exit__(self, *_: object) -> None:
            events.append(("exit", self.path))

    with database_offline_authority(
        database,
        lock_factory=RecordingLock,
        writer_scanner=lambda _: WriterScanResult((), ()),
    ):
        pass

    paths = database_authority_lock_paths(database)
    assert events == [
        *(("enter", path) for path in paths),
        *(("exit", path) for path in reversed(paths)),
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows authority root policy")
def test_windows_global_authority_uses_fixed_local_app_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_app_data = tmp_path / "Local"
    local_app_data.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    identity = service_coordination._global_authority_directory()

    assert identity.path == local_app_data / "dota2-predictor" / "authority"
    assert identity.path.is_dir()
    assert not (
        int(getattr(identity.path.lstat(), "st_file_attributes", 0))
        & service_coordination._WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point policy")
def test_directory_identity_rejects_windows_reparse_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "reparse"
    directory.mkdir()
    metadata = directory.lstat()
    fake_metadata = SimpleNamespace(
        st_mode=metadata.st_mode,
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino,
        st_file_attributes=(
            int(getattr(metadata, "st_file_attributes", 0))
            | service_coordination._WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        ),
    )
    real_lstat = Path.lstat

    def lstat(path: Path) -> object:
        if path.absolute() == directory.absolute():
            return fake_metadata
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat)

    with pytest.raises(RuntimeError, match="not a directory"):
        service_coordination.capture_directory_identity(
            directory,
            label="reparse authority",
        )


def test_lock_enter_baseexception_releases_registry_handle_and_os_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "interrupt.lock"

    class InterruptingRegistry(dict[Path, SingleInstanceLock]):
        def __setitem__(self, key: Path, value: SingleInstanceLock) -> None:
            super().__setitem__(key, value)
            raise KeyboardInterrupt

    interrupted_registry = InterruptingRegistry()
    monkeypatch.setattr(
        service_coordination,
        "_HELD_LOCKS",
        interrupted_registry,
    )
    with pytest.raises(KeyboardInterrupt):
        with SingleInstanceLock(lock_path):
            pytest.fail("interrupted lock registration reached the body")
    assert lock_path.absolute() not in interrupted_registry

    monkeypatch.setattr(service_coordination, "_HELD_LOCKS", {})
    with SingleInstanceLock(lock_path):
        pass


def test_lock_enter_cleanup_does_not_replace_baseexception_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "enter-cleanup.lock"
    owner_path = lock_path.with_name(f"{lock_path.name}.owner")

    class InterruptingRegistry(dict[Path, SingleInstanceLock]):
        def __setitem__(self, key: Path, value: SingleInstanceLock) -> None:
            super().__setitem__(key, value)
            raise KeyboardInterrupt("registration interrupted")

    monkeypatch.setattr(
        service_coordination,
        "_HELD_LOCKS",
        InterruptingRegistry(),
    )
    real_unlink = Path.unlink
    failed = False

    def unlink(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal failed
        if path.absolute() == owner_path.absolute() and not failed:
            failed = True
            raise RuntimeError("owner cleanup interrupted")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(KeyboardInterrupt, match="registration interrupted") as raised:
        with SingleInstanceLock(lock_path):
            pytest.fail("interrupted lock registration reached the body")

    assert any(
        "owner cleanup interrupted" in note
        for note in getattr(raised.value, "__notes__", ())
    )
    monkeypatch.setattr(service_coordination, "_HELD_LOCKS", {})
    with SingleInstanceLock(lock_path):
        pass


def test_lock_exit_cleanup_does_not_replace_body_baseexception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "exit-cleanup.lock"
    owner_path = lock_path.with_name(f"{lock_path.name}.owner")
    real_unlink = Path.unlink
    failed = False

    def unlink(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal failed
        if path.absolute() == owner_path.absolute() and not failed:
            failed = True
            raise RuntimeError("owner cleanup interrupted")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(KeyboardInterrupt, match="body interrupted") as raised:
        with SingleInstanceLock(lock_path):
            raise KeyboardInterrupt("body interrupted")

    assert any(
        "owner cleanup interrupted" in note
        for note in getattr(raised.value, "__notes__", ())
    )
    with SingleInstanceLock(lock_path):
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows marker replacement retry")
def test_windows_marker_retry_revalidates_after_sleep_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "bound.tmp"
    destination = tmp_path / "marker.json"
    malicious = tmp_path / "malicious.json"
    source.write_bytes(b"bound")
    destination.write_bytes(b"unbound")
    malicious.write_bytes(b"changed")
    metadata = destination.lstat()
    expected_identity = (int(metadata.st_dev), int(metadata.st_ino))
    parent_identity = service_coordination.capture_directory_identity(
        tmp_path,
        label="marker parent",
    )
    real_replace = os.replace
    replace_calls = 0

    def replace(left: object, right: object) -> None:
        nonlocal replace_calls
        if Path(left) == source and Path(right) == destination:
            replace_calls += 1
            if replace_calls == 1:
                raise PermissionError("marker is open")
        real_replace(left, right)

    def sleep(_: float) -> None:
        real_replace(malicious, destination)

    monkeypatch.setattr(service_coordination.os, "replace", replace)
    monkeypatch.setattr(service_coordination.time, "sleep", sleep)

    with pytest.raises(RuntimeError, match="changed during bind retry"):
        service_coordination._replace_polled_manager_marker(
            source,
            destination,
            expected_payload=b"unbound",
            expected_identity=expected_identity,
            parent_identity=parent_identity,
        )

    assert replace_calls == 1
    assert source.read_bytes() == b"bound"
    assert destination.read_bytes() == b"changed"


@pytest.mark.skipif(os.name != "nt", reason="Windows marker replacement read window")
def test_windows_bootstrap_retries_transient_marker_read_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker_path = (tmp_path / "manager-marker.json").resolve()
    marker_payload = {"marker_path": str(marker_path)}
    marker = json.dumps(marker_payload, sort_keys=True, separators=(",", ":"))
    bound_payload = {
        **marker_payload,
        "child_identity": {"pid": os.getpid(), "created_at": 1.0},
    }
    bound = json.dumps(bound_payload, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    attempts = 0

    def read_bytes(path: Path) -> bytes:
        nonlocal attempts
        assert path == marker_path
        attempts += 1
        if attempts == 1:
            raise PermissionError(13, "marker replacement in progress")
        return bound

    monkeypatch.setenv("DOTA2_MANAGER_CHILD_AUTHORITY_V1", marker)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    managed_child_bootstrap._wait_for_parent_binding()

    assert attempts == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows marker replacement read window")
def test_windows_bootstrap_permission_error_remains_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker_path = (tmp_path / "manager-marker.json").resolve()
    marker = json.dumps({"marker_path": str(marker_path)})
    monotonic_values = iter((10.0, 11.0))

    def read_bytes(path: Path) -> bytes:
        assert path == marker_path
        raise PermissionError(13, "marker remains unavailable")

    monkeypatch.setenv("DOTA2_MANAGER_CHILD_AUTHORITY_V1", marker)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(
        managed_child_bootstrap.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    with pytest.raises(RuntimeError, match="marker is unavailable") as raised:
        managed_child_bootstrap._wait_for_parent_binding()

    assert isinstance(raised.value.__cause__, PermissionError)


def test_bootstrap_non_permission_marker_error_is_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker_path = (tmp_path / "missing-marker.json").resolve()
    marker = json.dumps({"marker_path": str(marker_path)})
    attempts = 0

    def read_bytes(path: Path) -> bytes:
        nonlocal attempts
        assert path == marker_path
        attempts += 1
        raise FileNotFoundError(path)

    monkeypatch.setenv("DOTA2_MANAGER_CHILD_AUTHORITY_V1", marker)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    with pytest.raises(RuntimeError, match="marker is unavailable") as raised:
        managed_child_bootstrap._wait_for_parent_binding()

    assert isinstance(raised.value.__cause__, FileNotFoundError)
    assert attempts == 1


def test_web_fetch_cleanup_attempts_all_and_preserves_body_baseexception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "fetch-cleanup.db"
    database.touch()
    root = SimpleNamespace(
        pid=os.getpid(),
        create_time=lambda: psutil.Process(os.getpid()).create_time(),
        cmdline=lambda: [
            "python",
            "-m",
            "web.main",
            "--database",
            str(database.resolve()),
        ],
        environ=lambda: {"DATABASE_PATH": str(database.resolve())},
    )

    def process_factory(pid: int) -> object:
        if pid == os.getpid():
            return root
        raise KeyError(pid)

    cleanup_active = False
    marker_path: Path | None = None
    temporary: Path | None = None
    attempts: list[str] = []
    real_unlink = Path.unlink
    real_require_database = service_coordination.require_unique_database_file

    def unlink(path: Path, *args: object, **kwargs: object) -> None:
        if cleanup_active and path in {marker_path, temporary}:
            attempts.append("marker" if path == marker_path else "temporary")
            raise RuntimeError(f"{attempts[-1]} cleanup interrupted")
        real_unlink(path, *args, **kwargs)

    def require_database(*args: object, **kwargs: object) -> object:
        if cleanup_active:
            attempts.append("database")
            raise RuntimeError("database cleanup verification interrupted")
        return real_require_database(*args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(
        service_coordination,
        "require_unique_database_file",
        require_database,
    )

    with _held_web_authority(database):
        with pytest.raises(KeyboardInterrupt, match="fetch body interrupted") as raised:
            with web_fetch_child_authority(
                database,
                process_factory=process_factory,
            ) as environment:
                payload = json.loads(next(iter(environment.values())))
                marker_path = Path(str(payload["marker_path"]))
                temporary = marker_path.with_name(
                    f".{marker_path.name}.{os.getpid()}.tmp"
                )
                temporary.write_bytes(b"leftover")
                cleanup_active = True
                raise KeyboardInterrupt("fetch body interrupted")

        cleanup_active = False
        notes = tuple(getattr(raised.value, "__notes__", ()))
        assert {"temporary", "marker", "database"}.issubset(attempts)
        assert any("temporary cleanup interrupted" in note for note in notes)
        assert any("marker cleanup interrupted" in note for note in notes)
        assert any("database cleanup verification interrupted" in note for note in notes)
        assert marker_path is not None and marker_path.exists()
        assert temporary is not None and temporary.exists()
        real_unlink(marker_path)
        real_unlink(temporary)


def test_windows_command_match_normalizes_only_explicit_paths() -> None:
    expected = [
        r"C:\Python\Python.exe",
        "-u",
        r"Scripts\Worker.py",
        "--database",
        r"C:\Data\Dota.db",
        "--role",
        "Collector",
        "--url",
        "https://Host.invalid/Token",
        "--token",
        "AbC123",
        "/CaseSensitiveValue",
    ]
    path_case_only = [
        r"c:\python\PYTHON.EXE",
        "-u",
        r"scripts\worker.PY",
        "--database",
        r"c:\data\DOTA.DB",
        "--role",
        "Collector",
        "--url",
        "https://Host.invalid/Token",
        "--token",
        "AbC123",
        "/CaseSensitiveValue",
    ]
    assert _windows_commands_match(expected, path_case_only)

    for index in (6, 8, 10, 11):
        changed = list(path_case_only)
        changed[index] = changed[index].swapcase()
        assert not _windows_commands_match(expected, changed)

    module = [r"C:\Python\Python.exe", "-m", "Live_Betting.Monitor"]
    assert not _windows_commands_match(
        module,
        [r"c:\python\PYTHON.EXE", "-m", "live_betting.monitor"],
    )
OFFLINE_ENTRYPOINTS = (
    "scripts/backup_database.py",
    "scripts/compact_legacy_odds.py",
    "scripts/database_bundle.py",
    "scripts/database_cutover.py",
    "scripts/replay_browser_events.py",
    "scripts/restore_database.py",
)
READ_ONLY_ENTRYPOINTS = (
    "live_betting/report.py",
    "web/main.py",
)
DATABASE_ENTRYPOINTS = tuple(
    path
    for path in (*WRITER_ENTRYPOINTS, *OFFLINE_ENTRYPOINTS, *READ_ONLY_ENTRYPOINTS)
    if path != "scripts/replay_browser_events.py"
)
WRITER_AUTHORITY_TOKEN = {
    path: (
        "database_service_authority_lock_paths(args.database)"
        if path == "scripts/run_dota_shadow_service.py"
        else "database_writer_authority("
    )
    for path in WRITER_ENTRYPOINTS
}
OFFLINE_AUTHORITY_TOKEN = {
    "scripts/backup_database.py": "database_offline_authority(",
    "scripts/compact_legacy_odds.py": "compact_legacy_odds(",
    "scripts/database_bundle.py": "create_database_bundle(",
    "scripts/database_cutover.py": "_database_authority(",
    "scripts/replay_browser_events.py": "database_offline_authority(",
    "scripts/restore_database.py": "database_offline_authority(",
}
PLAIN_DATABASE_ARGUMENT = re.compile(
    r"\.add_argument\(\s*['\"]--database['\"]"
)


class SingleDatabaseArgumentTests(unittest.TestCase):
    @staticmethod
    def parser(
        *,
        default: Path | None = None,
        required: bool = False,
    ) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="database-test")
        options: dict[str, object] = {"required": required}
        if default is not None:
            options["default"] = default
        add_single_database_argument(parser, **options)
        return parser

    def test_equals_form_is_converted_to_path(self) -> None:
        args = self.parser().parse_args(["--database=selected.db"])

        self.assertEqual(args.database, Path("selected.db"))

    def test_mixed_duplicate_forms_fail_closed(self) -> None:
        parser = self.parser()
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            parser.parse_args(
                ["--database=first.db", "--database", "second.db"]
            )

        self.assertIn(
            "argument --database: may be specified only once",
            stderr.getvalue(),
        )

    def test_optional_default_and_required_single_value(self) -> None:
        default = Path("default.db")
        self.assertEqual(self.parser(default=default).parse_args([]).database, default)

        required = self.parser(required=True)
        self.assertEqual(
            required.parse_args(["--database", "required.db"]).database,
            Path("required.db"),
        )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            required.parse_args([])

    def test_parser_can_be_reused_with_fresh_namespaces(self) -> None:
        parser = self.parser()

        self.assertEqual(
            parser.parse_args(["--database=first.db"]).database,
            Path("first.db"),
        )
        self.assertEqual(
            parser.parse_args(["--database=second.db"]).database,
            Path("second.db"),
        )

    def test_database_entrypoints_cannot_bypass_shared_guard(self) -> None:
        for relative_path in DATABASE_ENTRYPOINTS:
            with self.subTest(entrypoint=relative_path):
                source = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("add_single_database_argument(", source)
                self.assertIsNone(PLAIN_DATABASE_ARGUMENT.search(source))


@pytest.mark.parametrize(
    ("relative_path", "authority_token"),
    sorted(WRITER_AUTHORITY_TOKEN.items()),
)
def test_every_classified_writer_invokes_lifetime_authority(
    relative_path: str,
    authority_token: str,
) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")

    assert authority_token in source


def test_database_entrypoint_authority_classification_is_disjoint() -> None:
    groups = (WRITER_ENTRYPOINTS, OFFLINE_ENTRYPOINTS, READ_ONLY_ENTRYPOINTS)
    flattened = [path for group in groups for path in group]

    assert len(flattened) == len(set(flattened))
    assert set(WRITER_AUTHORITY_TOKEN) == set(WRITER_ENTRYPOINTS)
    assert set(OFFLINE_AUTHORITY_TOKEN) == set(OFFLINE_ENTRYPOINTS)


@pytest.mark.parametrize(
    ("relative_path", "authority_token"),
    sorted(OFFLINE_AUTHORITY_TOKEN.items()),
)
def test_every_classified_offline_entrypoint_invokes_its_authority(
    relative_path: str,
    authority_token: str,
) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")

    assert authority_token in source


@pytest.mark.parametrize("lock_index", [0, 1], ids=["global-service", "service"])
def test_classified_writer_composition_performs_zero_writes_under_peer_lock(
    tmp_path: Path,
    lock_index: int,
) -> None:
    database = tmp_path / "candidate.db"
    database.touch()
    writes: list[str] = []

    lock_path = database_service_authority_lock_paths(database)[lock_index]
    with SingleInstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="already held"):
            with database_writer_authority(
                database,
                environ={},
                writer_scanner=lambda _: WriterScanResult((), ()),
            ):
                writes.append("business-write")

    assert writes == []


@pytest.mark.parametrize("lock_suffix", [".service.lock", ".web.lock"])
def test_offline_composition_performs_zero_mutations_under_peer_lock(
    tmp_path: Path,
    lock_suffix: str,
) -> None:
    database = tmp_path / "candidate.db"
    database.touch()
    mutations: list[str] = []

    with SingleInstanceLock(database.with_suffix(lock_suffix)):
        with pytest.raises(RuntimeError, match="already held"):
            with database_offline_authority(
                database,
                writer_scanner=lambda _: WriterScanResult((), ()),
            ):
                mutations.append("offline-mutation")

    assert mutations == []


class ProcessIdentityFailureTests(unittest.TestCase):
    def test_resolve_identity_rejects_five_millisecond_change(self) -> None:
        expected = ProcessIdentity(4100, 100.0)
        replacement = SimpleNamespace(
            pid=4100,
            create_time=lambda: 100.005,
            is_running=lambda: True,
            status=lambda: psutil.STATUS_RUNNING,
        )

        alive, process, detail = resolve_process_identity(
            expected,
            lambda _pid: replacement,
        )

        self.assertFalse(alive)
        self.assertIsNone(process)
        self.assertIsNone(detail)

    def test_resolve_identity_classifies_type_and_value_errors(self) -> None:
        expected = ProcessIdentity(4100, 100.0)

        for error in (TypeError("bad pid"), ValueError("bad time")):
            with self.subTest(error=type(error).__name__):
                def fail_identity(_pid: int) -> object:
                    raise error

                alive, process, detail = resolve_process_identity(
                    expected,
                    fail_identity,
                )
                self.assertIsNone(alive)
                self.assertIsNone(process)
                self.assertEqual(
                    detail,
                    f"identity_unverifiable:{type(error).__name__}",
                )

        invalid_time = SimpleNamespace(create_time=lambda: "not-a-time")
        alive, process, detail = resolve_process_identity(
            expected,
            lambda _pid: invalid_time,
        )
        self.assertIsNone(alive)
        self.assertIsNone(process)
        self.assertEqual(detail, "identity_unverifiable:ValueError")

    def test_subprocess_cleanup_classifies_type_and_value_errors(self) -> None:
        class Handle:
            pid = 4100

            @staticmethod
            def poll() -> None:
                return None

        for error in (TypeError("bad pid"), ValueError("bad time")):
            with self.subTest(error=type(error).__name__):
                def fail_identity(_pid: int) -> object:
                    raise error

                result = terminate_subprocess_tree(
                    Handle(),
                    process_factory=fail_identity,
                )
                self.assertFalse(result.ok)
                self.assertEqual(
                    result.detail,
                    f"subprocess_identity_unverifiable:{type(error).__name__}",
                )

        invalid_time = SimpleNamespace(create_time=lambda: None)
        result = terminate_subprocess_tree(
            Handle(),
            process_factory=lambda _pid: invalid_time,
        )
        self.assertFalse(result.ok)
        self.assertEqual(
            result.detail,
            "subprocess_identity_unverifiable:TypeError",
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows byte-range lock semantics")
def test_windows_lock_covers_the_complete_owner_record(tmp_path: Path) -> None:
    lock_path = tmp_path / "complete-record.lock"
    probe = (
        "import sys; from pathlib import Path; "
        "p=Path(sys.argv[1]); f=p.open('r+b', buffering=0); "
        "f.seek(128); f.write(b'forged')"
    )

    with SingleInstanceLock(lock_path) as lock:
        owner = lock.owner
        result = subprocess.run(
            [sys.executable, "-c", probe, str(lock_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode != 0
        assert "PermissionError" in result.stderr
        assert lock_path.stat().st_size == 4096
        sidecar = lock_path.with_name(f"{lock_path.name}.owner")
        payload = json.loads(sidecar.read_text(encoding="ascii"))
        assert payload["pid"] == owner.pid
        assert payload["created_at"] == owner.created_at
        assert payload["nonce"] == owner.nonce

    assert not sidecar.exists()
    persisted = json.loads(lock_path.read_bytes().rstrip(b" ").decode("ascii"))
    assert persisted["nonce"] == owner.nonce


def test_open_handles_cannot_issue_delegation_for_another_owner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate.db"
    database.touch()
    service = database_service_lock_path(database)
    web = database_web_lock_path(database)
    probe = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from live_betting.service_coordination import manager_child_authority",
            "db = Path(sys.argv[1])",
            "handles = [Path(value).open('rb') for value in sys.argv[2:4]]",
            "command = [sys.executable, '-c', 'pass', '--database', str(db)]",
            "with manager_child_authority(db, role='probe', command=command):",
            "    raise RuntimeError('delegation unexpectedly issued')",
        )
    )

    with SingleInstanceLock(service), SingleInstanceLock(web):
        result = subprocess.run(
            [sys.executable, "-c", probe, str(database), str(service), str(web)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    assert result.returncode != 0
    assert "owner token is unavailable" in result.stderr


def test_manager_marker_rejects_released_and_replaced_lock_owner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate.db"
    database.touch()
    command = [
        sys.executable,
        "-c",
        "pass",
        "--database",
        str(database.resolve()),
    ]
    root = psutil.Process(os.getpid())
    child = SimpleNamespace(
        pid=991_001,
        create_time=lambda: 991.0,
        cmdline=lambda: list(command),
    )

    def process_factory(pid: int) -> object:
        if pid == root.pid:
            return root
        if pid == child.pid:
            return child
        raise KeyError(pid)

    global_locks = [
        SingleInstanceLock(path)
        for path in database_global_authority_lock_paths(database)
    ]
    service_lock = SingleInstanceLock(database_service_lock_path(database))
    web_lock = SingleInstanceLock(database_web_lock_path(database))
    for global_lock in global_locks:
        global_lock.__enter__()
    service_lock.__enter__()
    web_lock.__enter__()
    authority_context = manager_child_authority(
        database,
        role="probe",
        command=command,
    )
    authority = authority_context.__enter__()
    marker = next(iter(authority.values()))
    marker_path = Path(json.loads(marker)["marker_path"])
    try:
        web_lock.__exit__(None, None, None)
        with pytest.raises(RuntimeError, match="not held by authority root"):
            _validate_manager_child_authority(
                database,
                marker,
                process_factory=process_factory,
                parent_pid=root.pid,
                current_pid=child.pid,
                lock_factory=SingleInstanceLock,
            )

        replacement = SingleInstanceLock(database_web_lock_path(database))
        replacement.__enter__()
        replacement_authority = manager_child_authority(
            database,
            role="probe",
            command=command,
        )
        replacement_authority.__enter__()
        try:
            with pytest.raises(RuntimeError, match="owner token differs"):
                _validate_manager_child_authority(
                    database,
                    marker,
                    process_factory=process_factory,
                    parent_pid=root.pid,
                    current_pid=child.pid,
                    lock_factory=SingleInstanceLock,
                )
        finally:
            replacement_authority.__exit__(None, None, None)
            replacement.__exit__(None, None, None)
    finally:
        with pytest.raises(RuntimeError, match="not held by authority root"):
            authority_context.__exit__(None, None, None)
        marker_path.unlink(missing_ok=True)
        service_lock.__exit__(None, None, None)
        for global_lock in reversed(global_locks):
            global_lock.__exit__(None, None, None)


def test_manager_marker_publication_interrupt_cleans_published_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    database.touch()
    command = [
        sys.executable,
        "-c",
        "pass",
        "--database",
        str(database.resolve()),
    ]
    real_replace = service_coordination.os.replace
    interrupted_path: Path | None = None

    def replace_then_interrupt(source: object, target: object) -> None:
        nonlocal interrupted_path
        real_replace(source, target)
        candidate = Path(os.fspath(target))
        if ".manager-child-authority." in candidate.name:
            interrupted_path = candidate
            raise KeyboardInterrupt

    with _held_database_authority(database):
        monkeypatch.setattr(
            service_coordination.os,
            "replace",
            replace_then_interrupt,
        )
        with pytest.raises(KeyboardInterrupt):
            with manager_child_authority(
                database,
                role="probe",
                command=command,
            ):
                pytest.fail("interrupted publication reached the child body")

    assert interrupted_path is not None
    assert not interrupted_path.exists()


def test_manager_marker_bind_interrupt_recovers_state_for_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    database.touch()
    command = [
        sys.executable,
        "-c",
        "pass",
        "--database",
        str(database.resolve()),
    ]
    child = SimpleNamespace(
        pid=991_002,
        create_time=lambda: 992.0,
        cmdline=lambda: list(command),
    )

    def process_factory(pid: int) -> object:
        if pid == child.pid:
            return child
        raise KeyError(pid)

    process_handle = SimpleNamespace(pid=child.pid, poll=lambda: None)
    with _held_database_authority(database):
        authority_context = manager_child_authority(
            database,
            role="probe",
            command=command,
        )
        authority = authority_context.__enter__()
        marker_path = Path(json.loads(next(iter(authority.values())))["marker_path"])
        real_replace = service_coordination.os.replace

        def bind_then_interrupt(source: object, target: object) -> None:
            real_replace(source, target)
            if Path(os.fspath(target)) == marker_path:
                raise KeyboardInterrupt

        monkeypatch.setattr(
            service_coordination.os,
            "replace",
            bind_then_interrupt,
        )
        try:
            with pytest.raises(KeyboardInterrupt):
                bind_manager_child_authority(
                    authority,
                    process_handle,
                    process_factory=process_factory,
                )
            assert authority.state.bound_identity == ProcessIdentity(child.pid, 992.0)
            assert authority.state.bound_bytes is not None
        finally:
            monkeypatch.setattr(service_coordination.os, "replace", real_replace)
            authority_context.__exit__(None, None, None)

    assert not marker_path.exists()


class ManagedWriterRecognitionTests(unittest.TestCase):
    def test_fetch_main_is_scanned_as_a_database_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()
            process = SimpleNamespace(info={
                "pid": 4200,
                "name": "python.exe",
                "cmdline": [
                    "python",
                    "-m",
                    "fetch.main",
                    "--database",
                    str(database.resolve()),
                ],
                "create_time": 100.0,
            })

            result = scan_managed_writers(
                database,
                process_iter=lambda _: [process],
            )

        self.assertEqual(result.conflicts, (ProcessIdentity(4200, 100.0),))
        self.assertEqual(result.unverifiable_pids, ())

    def test_offline_mode_fences_legacy_web_and_uvicorn_target_peers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()
            target = str(database.resolve())
            processes = [
                SimpleNamespace(info={
                    "pid": 4210,
                    "name": "python.exe",
                    "cmdline": [
                        "python",
                        "scripts/run_browser_companion.py",
                        "--database",
                        target,
                    ],
                    "create_time": 101.0,
                }),
                SimpleNamespace(info={
                    "pid": 4211,
                    "name": "python.exe",
                    "cmdline": [
                        "python",
                        "-m",
                        "web.main",
                        "--database",
                        target,
                    ],
                    "create_time": 102.0,
                    "environ": {"DATABASE_PATH": target},
                }),
                SimpleNamespace(info={
                    "pid": 4212,
                    "name": "uvicorn.exe",
                    "cmdline": ["uvicorn", "web.app:app"],
                    "create_time": 103.0,
                    "environ": {"DATABASE_PATH": target},
                }),
            ]

            result = scan_managed_writers(
                database,
                mode="offline",
                process_iter=lambda _: processes,
            )

        self.assertEqual(
            result.conflicts,
            (
                ProcessIdentity(4210, 101.0),
                ProcessIdentity(4211, 102.0),
                ProcessIdentity(4212, 103.0),
            ),
        )
        self.assertEqual(result.unverifiable_pids, ())

    def test_offline_mode_ignores_proven_other_database_and_rejects_unknown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            other = Path(directory) / "other.db"
            database.touch()
            other.touch()
            processes = [
                SimpleNamespace(info={
                    "pid": 4220,
                    "name": "python.exe",
                    "cmdline": [
                        "python",
                        "-m",
                        "web.main",
                        "--database",
                        str(other.resolve()),
                    ],
                    "create_time": 104.0,
                }),
                SimpleNamespace(info={
                    "pid": 4221,
                    "name": "python.exe",
                    "cmdline": ["python", "opaque_tool.py"],
                    "create_time": 105.0,
                }),
                SimpleNamespace(info={
                    "pid": 4222,
                    "name": "uvicorn.exe",
                    "cmdline": ["uvicorn", "web.app:app"],
                    "create_time": 106.0,
                }),
            ]

            result = scan_managed_writers(
                database,
                mode="offline",
                process_iter=lambda _: processes,
            )

        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.unverifiable_pids, (4221, 4222))


class WriterLifetimeAuthorityTests(unittest.TestCase):
    def test_real_vision_manager_redelegates_only_exact_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()
            watcher_probe = (
                "import sys; from pathlib import Path; "
                "from live_betting.service_coordination import "
                "database_writer_authority; "
                "db=Path(sys.argv[sys.argv.index('--database')+1]); "
                "authority=database_writer_authority(db); authority.__enter__(); "
                "print('nested-watcher-authorized', flush=True); "
                "authority.__exit__(None,None,None)"
            )
            manager_probe = "\n".join(
                (
                    "import subprocess, sys",
                    "from pathlib import Path",
                    "from live_betting.service_coordination import (",
                    "    bind_manager_child_authority,",
                    "    database_service_authority_lock_paths,",
                    "    database_writer_authority,",
                    "    delegated_writer_process_environment,",
                    "    manager_child_authority,",
                    ")",
                    "db = Path(sys.argv[sys.argv.index('--database') + 1])",
                    f"watcher_probe = {watcher_probe!r}",
                    "command = [sys.executable, '-c', watcher_probe, "
                    "           '--database', str(db.resolve())]",
                    "writer = database_writer_authority(db)",
                    "writer.__enter__()",
                    "delegate = delegated_writer_process_environment(",
                    "    db, role='vision_watcher', command=command)",
                    "environment = delegate.__enter__()",
                    "process = subprocess.Popen(command, env=environment, "
                    "                           stdout=subprocess.PIPE, "
                    "                           stderr=subprocess.PIPE, text=True)",
                    "bind_manager_child_authority(environment, process)",
                    "stdout, stderr = process.communicate(timeout=15)",
                    "delegate.__exit__(None, None, None)",
                    "try:",
                    "    manager_child_authority(",
                    "        db, role='vision_watcher', command=command,",
                    "        held_locks=database_service_authority_lock_paths(db),",
                    "    ).__enter__()",
                    "except RuntimeError as error:",
                    "    if str(error) != 'manager child authority root locks differ':",
                    "        raise",
                    "else:",
                    "    raise RuntimeError('root lock mismatch was accepted')",
                    "try:",
                    "    delegated_writer_process_environment(",
                    "        db, role='collector', command=command).__enter__()",
                    "except RuntimeError as error:",
                    "    if str(error) != 'manager child authority role is not delegated':",
                    "        raise",
                    "else:",
                    "    raise RuntimeError('undelegated role was accepted')",
                    "writer.__exit__(None, None, None)",
                    "sys.stdout.write(stdout)",
                    "sys.stderr.write(stderr)",
                    "raise SystemExit(process.returncode)",
                )
            )
            manager_command = [
                sys.executable,
                "-c",
                manager_probe,
                "--database",
                str(database.resolve()),
            ]
            with (
                _held_database_authority(database),
                manager_child_authority(
                    database,
                    role="vision_supervisor",
                    command=manager_command,
                    delegate_roles=("vision_watcher",),
                ) as authority,
            ):
                process = subprocess.Popen(
                    manager_command,
                    cwd=ROOT,
                    env=manager_child_process_environment(authority),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                bind_manager_child_authority(authority, process)
                stdout, stderr = process.communicate(timeout=20)

            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("nested-watcher-authorized", stdout)

    def test_real_manager_child_authority_is_exact_and_lock_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()
            probe = (
                "import sys; from pathlib import Path; "
                "from live_betting.service_coordination import "
                "database_writer_authority; "
                "db=Path(sys.argv[sys.argv.index('--database')+1]); "
                "authority=database_writer_authority(db); "
                "authority.__enter__(); print('authorized', flush=True); "
                "authority.__exit__(None,None,None)"
            )
            command = [
                sys.executable,
                "-c",
                probe,
                "--database",
                str(database.resolve()),
            ]
            global_locks = [
                SingleInstanceLock(path)
                for path in database_global_authority_lock_paths(database)
            ]
            service_lock = SingleInstanceLock(
                database_service_lock_path(database)
            )
            web_lock = SingleInstanceLock(database_web_lock_path(database))
            for global_lock in global_locks:
                global_lock.__enter__()
            service_lock.__enter__()
            web_lock.__enter__()
            authority_context = manager_child_authority(
                database,
                role="authority_probe",
                command=command,
            )
            authority = authority_context.__enter__()
            try:
                environment = manager_child_process_environment(authority)
                valid = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                bind_manager_child_authority(authority, valid)
                valid_stdout, valid_stderr = valid.communicate(timeout=15)
                self.assertEqual(valid.returncode, 0, valid_stderr)
                self.assertIn("authorized", valid_stdout)

                marker_name, marker = next(iter(authority.items()))
                payload = json.loads(marker)
                self.assertEqual(
                    tuple(
                        Path(str(owner["lock_path"])).resolve()
                        for owner in payload["root_lock_owners"]
                    ),
                    database_authority_lock_paths(database),
                )
                payload["role"] = "forged_role"
                forged_environment = dict(environment)
                forged_environment[marker_name] = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                forged_role = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=forged_environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertNotEqual(forged_role.returncode, 0)
                self.assertIn("marker differs", forged_role.stderr)

                forged_command = subprocess.run(
                    [*command, "unexpected-argument"],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertNotEqual(forged_command.returncode, 0)
                self.assertIn("child identity changed", forged_command.stderr)

                class Unrelated:
                    pid = 900_001

                    @staticmethod
                    def create_time() -> float:
                        return 1.0

                    @staticmethod
                    def ppid() -> int:
                        return 0

                class Child:
                    pid = 900_002

                    @staticmethod
                    def create_time() -> float:
                        return 2.0

                    @staticmethod
                    def cmdline() -> list[str]:
                        return list(command)

                root = psutil.Process(os.getpid())

                def process_factory(pid: int) -> object:
                    if pid == root.pid:
                        return root
                    if pid == Unrelated.pid:
                        return Unrelated()
                    if pid == Child.pid:
                        return Child()
                    raise KeyError(pid)

                with self.assertRaisesRegex(RuntimeError, "not an ancestor"):
                    with database_writer_authority(
                        database,
                        environ=authority,
                        process_factory=process_factory,
                        parent_pid=Unrelated.pid,
                        current_pid=Child.pid,
                    ):
                        pass
            finally:
                authority_context.__exit__(None, None, None)
                web_lock.__exit__(None, None, None)
                service_lock.__exit__(None, None, None)
                for global_lock in reversed(global_locks):
                    global_lock.__exit__(None, None, None)

    def test_child_waits_for_exact_parent_binding_and_rejects_rebind_and_pid_reuse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()
            probe = "\n".join(
                (
                    "import sys",
                    "from pathlib import Path",
                    "from live_betting.service_coordination import database_writer_authority",
                    "db = Path(sys.argv[sys.argv.index('--database') + 1])",
                    "print('child-started', flush=True)",
                    "with database_writer_authority(db):",
                    "    print('authorized-after-bind', flush=True)",
                )
            )
            command = [
                sys.executable,
                "-c",
                probe,
                "--database",
                str(database.resolve()),
            ]
            with (
                _held_database_authority(database),
                manager_child_authority(
                    database,
                    role="authority_probe",
                    command=command,
                ) as authority,
            ):
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=manager_child_process_environment(authority),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline().strip(), "child-started")
                time.sleep(0.05)
                identity = bind_manager_child_authority(authority, process)
                with self.assertRaisesRegex(RuntimeError, "already bound"):
                    bind_manager_child_authority(authority, process)

                root = psutil.Process(os.getpid())
                reused = SimpleNamespace(
                    pid=identity.pid,
                    create_time=lambda: identity.created_at + 0.005,
                    cmdline=lambda: list(command),
                )

                def process_factory(pid: int) -> object:
                    if pid == root.pid:
                        return root
                    if pid == identity.pid:
                        return reused
                    raise KeyError(pid)

                marker = next(iter(authority.values()))
                with self.assertRaisesRegex(RuntimeError, "child identity changed"):
                    _validate_manager_child_authority(
                        database,
                        marker,
                        process_factory=process_factory,
                        parent_pid=root.pid,
                        current_pid=identity.pid,
                        lock_factory=SingleInstanceLock,
                    )
                stdout, stderr = process.communicate(timeout=15)

            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("authorized-after-bind", stdout)

    def test_same_command_descendant_cannot_reuse_bound_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()
            probe = "\n".join(
                (
                    "import json, os, subprocess, sys",
                    "from pathlib import Path",
                    "from live_betting.service_coordination import database_writer_authority",
                    "db = Path(sys.argv[sys.argv.index('--database') + 1])",
                    "if os.environ.get('DOTA2_TEST_DESCENDANT') == '1':",
                    "    with database_writer_authority(db):",
                    "        print('descendant-authorized')",
                    "    raise SystemExit(0)",
                    "with database_writer_authority(db):",
                    "    environment = dict(os.environ)",
                    "    environment['DOTA2_TEST_DESCENDANT'] = '1'",
                    "    result = subprocess.run(json.loads(environment['DOTA2_TEST_COMMAND']),",
                    "                            env=environment, capture_output=True, text=True)",
                    "sys.stdout.write(result.stdout)",
                    "sys.stderr.write(result.stderr)",
                    "if result.returncode == 0 or 'child identity changed' not in result.stderr:",
                    "    raise SystemExit(7)",
                )
            )
            command = [
                sys.executable,
                "-c",
                probe,
                "--database",
                str(database.resolve()),
            ]
            with (
                _held_database_authority(database),
                manager_child_authority(
                    database,
                    role="authority_probe",
                    command=command,
                ) as authority,
            ):
                environment = manager_child_process_environment(authority)
                environment["DOTA2_TEST_COMMAND"] = json.dumps(command)
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                bind_manager_child_authority(authority, process)
                stdout, stderr = process.communicate(timeout=20)

            self.assertEqual(process.returncode, 0, stderr)
            self.assertNotIn("descendant-authorized", stdout)
            self.assertIn("child identity changed", stderr)

    def test_bootstrap_blocks_target_top_level_until_parent_binds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_path = Path(directory)
            database = root_path / "candidate.db"
            target = root_path / "target_side_effect.py"
            sibling = root_path / "target_sibling.py"
            effect = root_path / "target-ran.txt"
            database.touch()
            sibling.write_text("VALUE = 'ran'\n", encoding="utf-8")
            target.write_text(
                "import os, sys\n"
                "from pathlib import Path\n"
                "from target_sibling import VALUE\n"
                "Path(sys.argv[sys.argv.index('--effect') + 1]).write_text("
                "f'{VALUE}:{os.getpid()}', encoding='ascii')\n",
                encoding="utf-8",
            )
            command = managed_child_command(
                [
                    sys.executable,
                    str(target),
                    "--database",
                    str(database.resolve()),
                    "--effect",
                    str(effect),
                ]
            )
            with (
                _held_database_authority(database),
                manager_child_authority(
                    database,
                    role="authority_probe",
                    command=command,
                ) as authority,
            ):
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=manager_child_process_environment(authority),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                time.sleep(0.2)
                self.assertIsNone(process.poll())
                self.assertFalse(effect.exists())
                bind_manager_child_authority(authority, process)
                _, stderr = process.communicate(timeout=15)

            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(
                effect.read_text(encoding="ascii"),
                f"ran:{process.pid}",
            )

    def test_bootstrap_timeout_never_executes_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_path = Path(directory)
            database = root_path / "candidate.db"
            target = root_path / "target_side_effect.py"
            effect = root_path / "target-ran.txt"
            database.touch()
            target.write_text(
                "from pathlib import Path\n"
                f"Path({str(effect)!r}).write_text('ran', encoding='ascii')\n",
                encoding="utf-8",
            )
            command = managed_child_command(
                [
                    sys.executable,
                    str(target),
                    "--database",
                    str(database.resolve()),
                ]
            )
            with (
                _held_database_authority(database),
                manager_child_authority(
                    database,
                    role="authority_probe",
                    command=command,
                ) as authority,
            ):
                process = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=manager_child_process_environment(authority),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )

            self.assertNotEqual(process.returncode, 0)
            self.assertIn("binding timed out", process.stderr)
            self.assertFalse(effect.exists())

    def test_bootstrap_preserves_module_flags_and_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_path = Path(directory)
            database = root_path / "candidate.db"
            module = root_path / "bootstrap_module_probe.py"
            effect = root_path / "module-result.json"
            database.touch()
            module.write_text(
                "import json, sys\n"
                "from pathlib import Path\n"
                "effect = Path(sys.argv[sys.argv.index('--effect') + 1])\n"
                "effect.write_text(json.dumps({"
                "'argv': sys.argv[1:], "
                "'name': __name__, "
                "'write_through': sys.stdout.write_through"
                "}), encoding='utf-8')\n",
                encoding="utf-8",
            )
            command = managed_child_command(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "bootstrap_module_probe",
                    "--database",
                    str(database.resolve()),
                    "--effect",
                    str(effect),
                ]
            )
            self.assertEqual(command[1], "-u")
            with (
                _held_database_authority(database),
                manager_child_authority(
                    database,
                    role="authority_probe",
                    command=command,
                ) as authority,
            ):
                environment = manager_child_process_environment(authority)
                environment["PYTHONPATH"] = os.pathsep.join(
                    filter(
                        None,
                        (str(root_path), environment.get("PYTHONPATH", "")),
                    )
                )
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                bind_manager_child_authority(authority, process)
                _, stderr = process.communicate(timeout=15)

            self.assertEqual(process.returncode, 0, stderr)
            payload = json.loads(effect.read_text(encoding="utf-8"))
            self.assertEqual(payload["name"], "__main__")
            self.assertTrue(payload["write_through"])
            self.assertEqual(
                payload["argv"],
                [
                    "--database",
                    str(database.resolve()),
                    "--effect",
                    str(effect),
                ],
            )

    def test_bootstrap_propagates_target_exit_and_keyboard_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_path = Path(directory)
            database = root_path / "candidate.db"
            database.touch()
            cases = (
                ("raise SystemExit(7)\n", 7, ""),
                ("raise KeyboardInterrupt\n", None, "KeyboardInterrupt"),
            )
            for index, (source, expected_code, expected_error) in enumerate(cases):
                with self.subTest(index=index):
                    target = root_path / f"target_exit_{index}.py"
                    target.write_text(source, encoding="utf-8")
                    command = managed_child_command(
                        [
                            sys.executable,
                            str(target),
                            "--database",
                            str(database.resolve()),
                        ]
                    )
                    with (
                        _held_database_authority(database),
                        manager_child_authority(
                            database,
                            role="authority_probe",
                            command=command,
                        ) as authority,
                    ):
                        process = subprocess.Popen(
                            command,
                            cwd=ROOT,
                            env=manager_child_process_environment(authority),
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        bind_manager_child_authority(authority, process)
                        _, stderr = process.communicate(timeout=15)
                    if expected_code is None:
                        self.assertNotEqual(process.returncode, 0)
                    else:
                        self.assertEqual(process.returncode, expected_code, stderr)
                    self.assertIn(expected_error, stderr)

    def test_managed_child_command_rejects_inline_code_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()
            with self.assertRaisesRegex(ValueError, "inline code is unsupported"):
                managed_child_command(
                    [
                        sys.executable,
                        "-c",
                        "raise SystemExit(0)",
                        "--database",
                        str(database.resolve()),
                    ]
                )

    def test_managed_child_command_rejects_unreliable_interpreter_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_path = Path(directory)
            database = root_path / "candidate.db"
            target = root_path / "target.py"
            database.touch()
            target.write_text("pass\n", encoding="ascii")

            for flag in ("-I", "-S"):
                with self.subTest(flag=flag):
                    with self.assertRaisesRegex(
                        ValueError,
                        re.escape(f"Python flag {flag} is unsupported"),
                    ):
                        managed_child_command(
                            [
                                sys.executable,
                                flag,
                                str(target),
                                "--database",
                                str(database.resolve()),
                            ]
                        )

            command = managed_child_command(
                [
                    sys.executable,
                    "-s",
                    str(target),
                    "--database",
                    str(database.resolve()),
                ]
            )
            self.assertEqual(command[1], "-s")
            self.assertEqual(Path(command[2]), MANAGED_CHILD_BOOTSTRAP_SCRIPT)

    def test_managed_child_command_absolutizes_target_and_binds_both_databases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root_path = Path(directory)
            database = root_path / "candidate.db"
            other = root_path / "other.db"
            target = root_path / "target.py"
            database.touch()
            other.touch()
            target.write_text("pass\n", encoding="ascii")
            relative_target = os.path.relpath(target, Path.cwd())
            command = managed_child_command(
                [
                    sys.executable,
                    relative_target,
                    "--database",
                    str(database.resolve()),
                ]
            )
            wrapped_target = managed_child_target(command)
            self.assertIsNotNone(wrapped_target)
            assert wrapped_target is not None
            self.assertTrue(Path(wrapped_target[1]).is_absolute())
            self.assertEqual(Path(wrapped_target[1]), target.resolve())

            outer_mismatch = list(command)
            outer_mismatch[outer_mismatch.index("--database") + 1] = str(
                other.resolve()
            )
            with self.assertRaisesRegex(ValueError, "wrapper is invalid"):
                managed_child_command(outer_mismatch)

            inner_mismatch = list(command)
            sentinel = inner_mismatch.index("--target-argv")
            inner_database = inner_mismatch.index("--database", sentinel)
            inner_mismatch[inner_database + 1] = str(other.resolve())
            with self.assertRaisesRegex(ValueError, "wrapper is invalid"):
                managed_child_command(inner_mismatch)

    def test_live_betting_package_does_not_eagerly_import_models(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, live_betting; "
                "print('live_betting.models' in sys.modules)",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_manager_marker_hardlink_replacement_is_rejected_before_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()
            command = [
                sys.executable,
                "-c",
                "pass",
                "--database",
                str(database.resolve()),
            ]
            with (
                _held_database_authority(database),
                manager_child_authority(
                    database,
                    role="authority_probe",
                    command=command,
                ) as authority,
            ):
                marker = next(iter(authority.values()))
                marker_path = Path(json.loads(marker)["marker_path"])
                original = marker_path.with_name(f".{marker_path.name}.original")
                os.replace(marker_path, original)
                os.link(original, marker_path)
                try:
                    with self.assertRaisesRegex(RuntimeError, "file is unsafe"):
                        bind_manager_child_authority(
                            authority,
                            SimpleNamespace(pid=999_999),
                        )
                finally:
                    marker_path.unlink(missing_ok=True)
                    os.replace(original, marker_path)

    def test_manager_markers_cannot_be_exchanged_between_children(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()
            probe = (
                "import sys; from pathlib import Path; "
                "from live_betting.service_coordination import "
                "database_writer_authority; "
                "db=Path(sys.argv[sys.argv.index('--database')+1]); "
                "authority=database_writer_authority(db); "
                "authority.__enter__(); print('authorized', flush=True); "
                "authority.__exit__(None,None,None)"
            )
            commands = {
                "collector": [
                    sys.executable,
                    "-c",
                    probe,
                    "--database",
                    str(database.resolve()),
                    "--child",
                    "collector",
                ],
                "companion": [
                    sys.executable,
                    "-c",
                    probe,
                    "--database",
                    str(database.resolve()),
                    "--child",
                    "companion",
                ],
            }
            with (
                _held_database_authority(database),
                manager_child_authority(
                    database,
                    role="collector",
                    command=commands["collector"],
                ) as collector_authority,
                manager_child_authority(
                    database,
                    role="companion",
                    command=commands["companion"],
                ) as companion_authority,
            ):
                authorities = {
                    "collector": collector_authority,
                    "companion": companion_authority,
                }
                for name, command in commands.items():
                    valid = subprocess.Popen(
                        command,
                        cwd=ROOT,
                        env=manager_child_process_environment(authorities[name]),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    bind_manager_child_authority(authorities[name], valid)
                    valid_stdout, valid_stderr = valid.communicate(timeout=15)
                    self.assertEqual(valid.returncode, 0, valid_stderr)
                    self.assertIn("authorized", valid_stdout)

                for name, command in commands.items():
                    other = "companion" if name == "collector" else "collector"
                    exchanged = subprocess.run(
                        command,
                        cwd=ROOT,
                        env=manager_child_process_environment(authorities[other]),
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    self.assertNotEqual(exchanged.returncode, 0)
                    self.assertIn("child identity changed", exchanged.stderr)

    def test_legacy_v1_supervisor_authority_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()

            with self.assertRaisesRegex(
                RuntimeError,
                "legacy supervisor child authority is unsupported",
            ):
                with database_writer_authority(
                    database,
                    environ={"DOTA2_SUPERVISOR_AUTHORITY_V1": "legacy-broad-marker"},
                ):
                    self.fail("legacy broad marker reached writer body")

    def test_offline_authority_holds_service_and_web_locks_for_lifetime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()

            with database_offline_authority(
                database,
                writer_scanner=lambda _: WriterScanResult((), ()),
            ):
                for lock_path in database_authority_lock_paths(database):
                    with self.assertRaisesRegex(RuntimeError, "already held"):
                        with SingleInstanceLock(lock_path):
                            pass

            with _held_database_authority(database):
                pass

    def test_standalone_writer_holds_service_lock_for_lifetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()

            with database_writer_authority(database, environ={}):
                for lock_path in database_service_authority_lock_paths(database):
                    with self.assertRaisesRegex(RuntimeError, "already held"):
                        with SingleInstanceLock(lock_path):
                            pass

            with SingleInstanceLock(database_service_lock_path(database)):
                pass

    def test_direct_writer_can_coexist_with_web_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()
            entered = False

            with _held_web_authority(database):
                with database_writer_authority(
                    database,
                    environ={},
                    writer_scanner=lambda _: WriterScanResult((), ()),
                ):
                    entered = True

            self.assertTrue(entered)

    def test_valid_web_fetch_marker_allows_only_fetch_under_web_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()

            class Root:
                pid = os.getpid()

                @staticmethod
                def create_time() -> float:
                    return psutil.Process(os.getpid()).create_time()

                @staticmethod
                def cmdline() -> list[str]:
                    return [
                        "python",
                        "-m",
                        "web.main",
                        "--database",
                        str(database.resolve()),
                    ]

                @staticmethod
                def environ() -> dict[str, str]:
                    return {"DATABASE_PATH": str(database.resolve())}

            class Fetch:
                pid = 5200

                @staticmethod
                def create_time() -> float:
                    return 200.0

                @staticmethod
                def cmdline() -> list[str]:
                    return [
                        "python",
                        "-m",
                        "fetch.main",
                        "--database",
                        str(database.resolve()),
                    ]

            root = Root()
            fetch = Fetch()

            def process_factory(pid: int) -> object:
                if pid in {os.getpid(), root.pid}:
                    return root
                if pid == fetch.pid:
                    return fetch
                raise KeyError(pid)

            with _held_web_authority(database):
                with web_fetch_child_authority(
                    database,
                    process_factory=process_factory,
                ) as environment:
                    child_environment = web_fetch_process_environment(
                        environment,
                        environ={
                            "DOTA2_SUPERVISOR_AUTHORITY_V1": "broader-marker",
                            "PATH": "test-path",
                        },
                    )
                    self.assertNotIn(
                        "DOTA2_SUPERVISOR_AUTHORITY_V1",
                        child_environment,
                    )
                    self.assertEqual(child_environment["PATH"], "test-path")
                    self.assertEqual(
                        child_environment[next(iter(environment))],
                        next(iter(environment.values())),
                    )
                    with database_writer_authority(
                        database,
                        environ=environment,
                        process_factory=process_factory,
                        parent_pid=root.pid,
                        current_pid=fetch.pid,
                        writer_scanner=lambda _: WriterScanResult((), ()),
                    ):
                        with self.assertRaisesRegex(RuntimeError, "already held"):
                            with SingleInstanceLock(
                                database_service_lock_path(database)
                            ):
                                pass

                    marker_name = next(iter(environment))
                    payload = json.loads(environment[marker_name])
                    payload["role"] = "score"
                    forged = {
                        marker_name: json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    }
                    with self.assertRaisesRegex(RuntimeError, "role differs"):
                        with database_writer_authority(
                            database,
                            environ=forged,
                            process_factory=process_factory,
                            parent_pid=root.pid,
                            current_pid=fetch.pid,
                        ):
                            pass

                    class WrongWriter(Fetch):
                        @staticmethod
                        def cmdline() -> list[str]:
                            return [
                                "python",
                                "scripts/score_strict_event_players.py",
                                "--database",
                                str(database.resolve()),
                            ]

                    wrong = WrongWriter()

                    def wrong_factory(pid: int) -> object:
                        if pid in {os.getpid(), root.pid}:
                            return root
                        if pid == fetch.pid:
                            return wrong
                        raise KeyError(pid)

                    with self.assertRaisesRegex(RuntimeError, "command differs"):
                        with database_writer_authority(
                            database,
                            environ=environment,
                            process_factory=wrong_factory,
                            parent_pid=root.pid,
                            current_pid=fetch.pid,
                        ):
                            pass

    def test_web_fetch_marker_rejects_nonancestor_dead_and_reused_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()

            class Process:
                def __init__(self, pid: int, created_at: float, parent: int) -> None:
                    self.pid = pid
                    self._created_at = created_at
                    self._parent = parent

                def create_time(self) -> float:
                    return self._created_at

                def ppid(self) -> int:
                    return self._parent

                def cmdline(self) -> list[str]:
                    if self.pid == root.pid:
                        return [
                            "python",
                            "-m",
                            "web.main",
                            "--database",
                            str(database.resolve()),
                        ]
                    return [
                        "python",
                        "-m",
                        "fetch.main",
                        "--database",
                        str(database.resolve()),
                    ]

                def environ(self) -> dict[str, str]:
                    return {"DATABASE_PATH": str(database.resolve())}

            root = Process(
                os.getpid(),
                psutil.Process(os.getpid()).create_time(),
                0,
            )
            unrelated = Process(5300, 300.0, 0)
            fetch = Process(5200, 200.0, root.pid)
            root_mode = "alive"

            def process_factory(pid: int) -> object:
                if pid == unrelated.pid:
                    return unrelated
                if pid == fetch.pid:
                    return fetch
                if pid == root.pid:
                    if root_mode == "dead":
                        raise KeyError(pid)
                    if root_mode == "reused":
                        return Process(root.pid, root._created_at + 1.0, 0)
                    return root
                raise KeyError(pid)

            with _held_web_authority(database):
                with web_fetch_child_authority(
                    database,
                    process_factory=process_factory,
                ) as environment:
                    with self.assertRaisesRegex(RuntimeError, "not an ancestor"):
                        with database_writer_authority(
                            database,
                            environ=environment,
                            process_factory=process_factory,
                            parent_pid=unrelated.pid,
                            current_pid=fetch.pid,
                        ):
                            pass

                    root_mode = "dead"
                    with self.assertRaisesRegex(RuntimeError, "unverifiable"):
                        with database_writer_authority(
                            database,
                            environ=environment,
                            process_factory=process_factory,
                            parent_pid=root.pid,
                            current_pid=fetch.pid,
                        ):
                            pass

                    root_mode = "reused"
                    with self.assertRaisesRegex(RuntimeError, "identity changed"):
                        with database_writer_authority(
                            database,
                            environ=environment,
                            process_factory=process_factory,
                            parent_pid=root.pid,
                            current_pid=fetch.pid,
                        ):
                            pass

    def test_standalone_writer_rejects_an_orphan_after_acquiring_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            database.touch()
            conflict = ProcessIdentity(4230, 99.0)

            with self.assertRaisesRegex(RuntimeError, "4230"):
                with database_writer_authority(
                    database,
                    environ={},
                    writer_scanner=lambda _: WriterScanResult((conflict,), ()),
                ):
                    self.fail("orphan conflict reached writer body")

            with SingleInstanceLock(database_service_lock_path(database)):
                pass

if __name__ == "__main__":
    unittest.main()
