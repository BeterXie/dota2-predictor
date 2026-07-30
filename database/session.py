"""PostgreSQL runtime session with SQLAlchemy Core transaction semantics."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, overload

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


_WRITE_PREFIX = re.compile(r"^\s*(INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)


class DatabaseRow:
    """Buffered row with sqlite-style numeric and named access."""

    __slots__ = ("_index", "_keys", "_values")

    def __init__(self, keys: Sequence[str], values: Sequence[Any]) -> None:
        self._keys = tuple(keys)
        self._values = tuple(values)
        self._index = {key: index for index, key in enumerate(self._keys)}

    @overload
    def __getitem__(self, key: int) -> Any: ...

    @overload
    def __getitem__(self, key: slice) -> tuple[Any, ...]: ...

    @overload
    def __getitem__(self, key: str) -> Any: ...

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> tuple[str, ...]:
        return self._keys


@dataclass
class DatabaseResult:
    rows: tuple[DatabaseRow, ...]
    rowcount: int
    columns: tuple[str, ...] = ()
    _position: int = 0

    def __iter__(self) -> Iterator[DatabaseRow]:
        while (row := self.fetchone()) is not None:
            yield row

    @property
    def description(self) -> tuple[tuple[str], ...]:
        return tuple((column,) for column in self.columns)

    def fetchone(self) -> DatabaseRow | None:
        if self._position >= len(self.rows):
            return None
        row = self.rows[self._position]
        self._position += 1
        return row

    def fetchall(self) -> list[DatabaseRow]:
        rows = list(self.rows[self._position :])
        self._position = len(self.rows)
        return rows

    def scalar_one(self) -> Any:
        if len(self.rows) != 1 or len(self.rows[0]) != 1:
            raise RuntimeError("query did not return exactly one scalar")
        return self.rows[0][0]


class _State(threading.local):
    connection: Connection | None = None
    explicit_depth: int = 0


class PostgresSession:
    """A thread-local DBAPI-shaped session backed only by PostgreSQL."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._state = _State()

    @property
    def in_transaction(self) -> bool:
        connection = self._state.connection
        return connection is not None and connection.in_transaction()

    @property
    def active_connection(self) -> Connection:
        """Return the connection owned by the current explicit transaction."""

        connection = self._state.connection
        if connection is None or self._state.explicit_depth == 0:
            raise RuntimeError("an explicit database transaction is required")
        return connection

    def execute(
        self,
        statement: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> DatabaseResult:
        sql, bound = _bind_parameters(statement, parameters)
        is_write = _WRITE_PREFIX.match(sql) is not None
        transient = self._state.connection is None and not is_write
        connection = self._state.connection or self.engine.connect()
        if is_write and self._state.connection is None:
            self._state.connection = connection
        try:
            result = connection.execute(text(sql), bound)
            keys = tuple(result.keys()) if result.returns_rows else ()
            rows = (
                tuple(DatabaseRow(keys, tuple(row)) for row in result.fetchall())
                if result.returns_rows
                else ()
            )
            return DatabaseResult(rows=rows, rowcount=result.rowcount, columns=keys)
        except BaseException:
            if transient:
                connection.rollback()
                connection.close()
            raise
        finally:
            if transient:
                connection.rollback()
                connection.close()

    def executemany(
        self,
        statement: str,
        parameter_rows: Sequence[Sequence[Any] | Mapping[str, Any]],
    ) -> DatabaseResult:
        if not parameter_rows:
            return DatabaseResult(rows=(), rowcount=0)
        first_sql, first_bound = _bind_parameters(statement, parameter_rows[0])
        bound_rows = [first_bound]
        for parameters in parameter_rows[1:]:
            sql, bound = _bind_parameters(statement, parameters)
            if sql != first_sql:
                raise ValueError("executemany produced inconsistent SQL")
            bound_rows.append(bound)
        connection = self._state.connection or self.engine.connect()
        if self._state.connection is None:
            self._state.connection = connection
        result = connection.execute(text(first_sql), bound_rows)
        return DatabaseResult(rows=(), rowcount=result.rowcount)

    def commit(self) -> None:
        connection = self._state.connection
        if connection is None:
            return
        try:
            connection.commit()
        finally:
            connection.close()
            self._state.connection = None

    def begin(self) -> None:
        """Begin one DBAPI-style transaction for legacy Core call sites."""

        if self._state.connection is not None:
            raise RuntimeError("database transaction is already active")
        connection = self.engine.connect()
        connection.begin()
        self._state.connection = connection

    def rollback(self) -> None:
        connection = self._state.connection
        if connection is None:
            return
        try:
            connection.rollback()
        finally:
            connection.close()
            self._state.connection = None

    def close(self) -> None:
        self.rollback()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._state.connection is None:
            connection = self.engine.connect()
            self._state.connection = connection
            transaction = connection.begin()
            outermost = True
        else:
            connection = self._state.connection
            transaction = connection.begin_nested()
            outermost = False
        self._state.explicit_depth += 1
        try:
            yield
        except BaseException:
            transaction.rollback()
            raise
        else:
            transaction.commit()
        finally:
            self._state.explicit_depth -= 1
            if outermost:
                connection.close()
                self._state.connection = None


def _bind_parameters(
    statement: str,
    parameters: Sequence[Any] | Mapping[str, Any],
) -> tuple[str, Mapping[str, Any]]:
    if isinstance(parameters, Mapping):
        return statement, parameters
    values = tuple(parameters)
    if not values:
        return statement, {}
    output: list[str] = []
    bound: dict[str, Any] = {}
    value_index = 0
    quote: str | None = None
    index = 0
    while index < len(statement):
        character = statement[index]
        if quote is not None:
            output.append(character)
            if character == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    output.append(statement[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in ("'", '"'):
            quote = character
            output.append(character)
        elif character == "?":
            if value_index >= len(values):
                raise ValueError("not enough SQL parameters")
            name = f"p{value_index}"
            output.append(f":{name}")
            bound[name] = values[value_index]
            value_index += 1
        else:
            output.append(character)
        index += 1
    if value_index != len(values):
        raise ValueError("too many SQL parameters")
    return "".join(output), bound
