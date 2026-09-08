"""SQLite persistence for immutable lab strategy and experiment records."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from numbers import Real
from pathlib import Path
from typing import TypeAlias, cast

JsonDict: TypeAlias = dict[str, object]


class LabConflictError(ValueError):
    """An optimistic version write did not match the current revision."""


class TraceTimestampUnavailableError(ValueError):
    """A trace time filter was requested for an un-timestamped result."""


class NonFiniteNativeResultError(ValueError):
    """A native result contains NaN or infinity and cannot be research evidence."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Mapping[str, object]) -> str:
    _assert_finite_numbers(value)
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _assert_finite_numbers(value: object, path: str = "result") -> None:
    if isinstance(value, Real) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise NonFiniteNativeResultError(
                f"native result contains non-finite number at {path}; "
                "publish undefined statistics as null with a reason instead"
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_finite_numbers(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_finite_numbers(item, f"{path}[{index}]")


def _object(value: str) -> JsonDict:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("stored lab JSON must be an object")
    return cast(JsonDict, parsed)


def _trace_timestamp(trace: Mapping[str, object]) -> str | None:
    raw = next(
        (
            trace[key]
            for key in ("known_at", "timestamp", "time")
            if isinstance(trace.get(key), str)
        ),
        None,
    )
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


class LabRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_strategy(self, name: str, description: str, clone_of: str | None) -> JsonDict:
        strategy_id = uuid.uuid4().hex
        created_at = _now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO lab_strategies(strategy_id,name,description,clone_of,revision,created_at,updated_at) VALUES(?,?,?,?,0,?,?)",
                (strategy_id, name, description, clone_of, created_at, created_at),
            )
        return self.get_strategy(strategy_id)

    def get_strategy(self, strategy_id: str) -> JsonDict:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM lab_strategies WHERE strategy_id=?", (strategy_id,)
            ).fetchone()
        if row is None:
            raise KeyError(strategy_id)
        return dict(row)

    def list_strategies(self) -> list[JsonDict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM lab_strategies ORDER BY updated_at DESC, strategy_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_version(
        self, strategy_id: str, expected_revision: int, content: Mapping[str, object], message: str
    ) -> JsonDict:
        version_id = uuid.uuid4().hex
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM lab_strategies WHERE strategy_id=?", (strategy_id,)
            ).fetchone()
            if row is None:
                raise KeyError(strategy_id)
            revision = int(row["revision"])
            if revision != expected_revision:
                raise LabConflictError(
                    f"expected revision {expected_revision}, current revision is {revision}"
                )
            next_revision = revision + 1
            connection.execute(
                "INSERT INTO lab_strategy_versions(version_id,strategy_id,revision,content_json,message,created_at) VALUES(?,?,?,?,?,?)",
                (version_id, strategy_id, next_revision, _json(content), message, now),
            )
            connection.execute(
                "UPDATE lab_strategies SET revision=?,updated_at=? WHERE strategy_id=?",
                (next_revision, now, strategy_id),
            )
        return self.get_version(version_id)

    def get_version(self, version_id: str) -> JsonDict:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM lab_strategy_versions WHERE version_id=?", (version_id,)
            ).fetchone()
        if row is None:
            raise KeyError(version_id)
        return {
            "version_id": str(row["version_id"]),
            "strategy_id": str(row["strategy_id"]),
            "revision": int(row["revision"]),
            "content": _object(str(row["content_json"])),
            "message": str(row["message"]),
            "created_at": str(row["created_at"]),
        }

    def list_versions(self, strategy_id: str) -> list[JsonDict]:
        self.get_strategy(strategy_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM lab_strategy_versions WHERE strategy_id=? ORDER BY revision DESC",
                (strategy_id,),
            ).fetchall()
        return [
            {
                "version_id": str(row["version_id"]),
                "strategy_id": str(row["strategy_id"]),
                "revision": int(row["revision"]),
                "content": _object(str(row["content_json"])),
                "message": str(row["message"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def create_note(self, note: Mapping[str, object]) -> JsonDict:
        note_id = uuid.uuid4().hex
        now = _now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO lab_notes(note_id,body,entity_type,entity_id,strategy_version_id,experiment_id,result_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    note_id,
                    note["body"],
                    note["entity_type"],
                    note["entity_id"],
                    note["strategy_version_id"],
                    note.get("experiment_id"),
                    note.get("result_id"),
                    now,
                    now,
                ),
            )
        return self.get_note(note_id)

    def get_note(self, note_id: str) -> JsonDict:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM lab_notes WHERE note_id=?", (note_id,)
            ).fetchone()
        if row is None:
            raise KeyError(note_id)
        return dict(row)

    def list_notes(self, strategy_version_id: str | None = None) -> list[JsonDict]:
        query = "SELECT * FROM lab_notes"
        values: tuple[object, ...] = ()
        if strategy_version_id:
            query += " WHERE strategy_version_id=?"
            values = (strategy_version_id,)
        query += " ORDER BY updated_at DESC"
        with self._connection() as connection:
            rows = connection.execute(query, values).fetchall()
        return [dict(row) for row in rows]

    def update_note(self, note_id: str, body: str) -> JsonDict:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE lab_notes SET body=?,updated_at=? WHERE note_id=?", (body, _now(), note_id)
            )
        if cursor.rowcount != 1:
            raise KeyError(note_id)
        return self.get_note(note_id)

    def delete_note(self, note_id: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM lab_notes WHERE note_id=?", (note_id,))
        if cursor.rowcount != 1:
            raise KeyError(note_id)

    def add_trial(
        self,
        job_id: str,
        number: int,
        parameters: Mapping[str, object],
        state: str,
        value: float | None,
        result: Mapping[str, object] | None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO lab_optimization_trials(job_id,trial_number,parameters_json,state,value,result_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    job_id,
                    number,
                    _json(parameters),
                    state,
                    value,
                    _json(result) if result is not None else None,
                    _now(),
                ),
            )

    def list_trials(self, job_id: str) -> list[JsonDict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM lab_optimization_trials WHERE job_id=? ORDER BY trial_number",
                (job_id,),
            ).fetchall()
        return [
            {
                "trial_number": int(row["trial_number"]),
                "parameters": _object(str(row["parameters_json"])),
                "state": str(row["state"]),
                "value": row["value"],
                "result": _object(str(row["result_json"])) if row["result_json"] else None,
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def store_result(
        self, result: Mapping[str, object], traces: list[Mapping[str, object]]
    ) -> JsonDict:
        result_id = str(result.get("result_id") or result.get("run_id") or uuid.uuid4().hex)
        group = _comparability(result)
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO lab_results(result_id,summary_json,comparability_json,created_at) VALUES(?,?,?,?)",
                (result_id, _json(result), _json(group), _now()),
            )
            connection.execute("DELETE FROM lab_decision_traces WHERE result_id=?", (result_id,))
            for sequence, trace in enumerate(traces):
                connection.execute(
                    "INSERT INTO lab_decision_traces(result_id,sequence,known_at,trace_json) VALUES(?,?,?,?)",
                    (result_id, sequence, _trace_timestamp(trace), _json(trace)),
                )
        return self.get_result(result_id)

    def get_result(self, result_id: str) -> JsonDict:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM lab_results WHERE result_id=?", (result_id,)
            ).fetchone()
            if row is None:
                try:
                    native = connection.execute(
                        "SELECT summary_json,created_at FROM research_runs WHERE run_id=?",
                        (result_id,),
                    ).fetchone()
                except sqlite3.OperationalError:
                    native = None
                if native is None:
                    raise KeyError(result_id)
                summary = _object(str(native["summary_json"]))
                return {
                    "result_id": result_id,
                    "summary": summary,
                    "comparability_group": _comparability(summary),
                    "created_at": str(native["created_at"]),
                    "source": "research_runs",
                }
        return {
            "result_id": str(row["result_id"]),
            "summary": _object(str(row["summary_json"])),
            "comparability_group": _object(str(row["comparability_json"])),
            "created_at": str(row["created_at"]),
        }

    def list_results(self, *, limit: int = 50) -> list[JsonDict]:
        """Return a bounded lab/native result index without owning the job queue."""

        bounded = max(1, min(limit, 100))
        with self._connection() as connection:
            lab_rows = connection.execute(
                "SELECT result_id,summary_json,comparability_json,created_at FROM lab_results "
                "ORDER BY created_at DESC LIMIT ?",
                (bounded,),
            ).fetchall()
            result_ids = {str(row["result_id"]) for row in lab_rows}
            try:
                native_rows = connection.execute(
                    "SELECT run_id,summary_json,created_at FROM research_runs "
                    "ORDER BY created_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
            except sqlite3.OperationalError:
                native_rows = []
        rows: list[JsonDict] = [
            {
                "result_id": str(row["result_id"]),
                "summary": _object(str(row["summary_json"])),
                "comparability_group": _object(str(row["comparability_json"])),
                "created_at": str(row["created_at"]),
                "source": "lab_projection",
            }
            for row in lab_rows
        ]
        rows.extend(
            {
                "result_id": str(row["run_id"]),
                "summary": _object(str(row["summary_json"])),
                "comparability_group": _comparability(_object(str(row["summary_json"]))),
                "created_at": str(row["created_at"]),
                "source": "research_runs",
            }
            for row in native_rows
            if str(row["run_id"]) not in result_ids
        )
        return sorted(rows, key=lambda row: str(row["created_at"]), reverse=True)[:bounded]

    def traces(
        self,
        result_id: str,
        cursor: int | None,
        window: int,
        *,
        from_time: str | None = None,
        to_time: str | None = None,
    ) -> JsonDict:
        start = cursor if cursor is not None else 0
        filters = ""
        values: list[object] = [result_id, start]
        if from_time is not None:
            filters += " AND known_at>=?"
            values.append(from_time)
        if to_time is not None:
            filters += " AND known_at<=?"
            values.append(to_time)
        values.append(window + 1)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT sequence,known_at,trace_json FROM lab_decision_traces "
                "WHERE result_id=? AND sequence>=?" + filters + " ORDER BY sequence LIMIT ?",
                values,
            ).fetchall()
            if not rows:
                try:
                    native = connection.execute(
                        "SELECT summary_json FROM research_runs WHERE run_id=?", (result_id,)
                    ).fetchone()
                except sqlite3.OperationalError:
                    native = None
                if native is not None:
                    summary = _object(str(native["summary_json"]))
                    raw = summary.get("decision_traces", [])
                    if isinstance(raw, list):
                        all_items = [item for item in raw if isinstance(item, dict)]
                        if from_time is not None or to_time is not None:
                            timestamped = [
                                (item, _trace_timestamp(item)) for item in all_items
                            ]
                            if not any(timestamp for _, timestamp in timestamped):
                                raise TraceTimestampUnavailableError(
                                    "trace timestamps are unavailable for this result"
                                )
                            all_items = [
                                item
                                for item, timestamp in timestamped
                                if timestamp is not None
                                and (from_time is None or timestamp >= from_time)
                                and (to_time is None or timestamp <= to_time)
                            ]
                        items = all_items[start : start + window]
                        next_cursor = start + window if len(all_items) > start + window else None
                        return {
                            "items": [
                                {"sequence": start + index, "trace": item}
                                for index, item in enumerate(items)
                            ],
                            "next_cursor": next_cursor,
                            "source": "research_runs",
                        }
            if (from_time is not None or to_time is not None) and not rows:
                timestamped = connection.execute(
                    "SELECT 1 FROM lab_decision_traces WHERE result_id=? AND known_at IS NOT NULL LIMIT 1",
                    (result_id,),
                ).fetchone()
                if timestamped is None:
                    raise TraceTimestampUnavailableError(
                        "trace timestamps are unavailable for this result"
                    )
        items = rows[:window]
        return {
            "items": [
                {
                    "sequence": int(row["sequence"]),
                    "known_at": row["known_at"],
                    "trace": _object(str(row["trace_json"])),
                }
                for row in items
            ],
            "next_cursor": int(rows[window]["sequence"]) if len(rows) > window else None,
        }

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS lab_strategies(strategy_id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL,clone_of TEXT,revision INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lab_strategy_versions(version_id TEXT PRIMARY KEY,strategy_id TEXT NOT NULL,revision INTEGER NOT NULL,content_json TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(strategy_id,revision));
            CREATE TABLE IF NOT EXISTS lab_notes(note_id TEXT PRIMARY KEY,body TEXT NOT NULL,entity_type TEXT NOT NULL DEFAULT 'strategy_version',entity_id TEXT NOT NULL DEFAULT '',strategy_version_id TEXT NOT NULL,experiment_id TEXT,result_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lab_optimization_trials(job_id TEXT NOT NULL,trial_number INTEGER NOT NULL,parameters_json TEXT NOT NULL,state TEXT NOT NULL,value REAL,result_json TEXT,created_at TEXT NOT NULL,PRIMARY KEY(job_id,trial_number));
            CREATE TABLE IF NOT EXISTS lab_results(result_id TEXT PRIMARY KEY,summary_json TEXT NOT NULL,comparability_json TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lab_decision_traces(result_id TEXT NOT NULL,sequence INTEGER NOT NULL,known_at TEXT,trace_json TEXT NOT NULL,PRIMARY KEY(result_id,sequence));
            CREATE INDEX IF NOT EXISTS ix_lab_traces_result ON lab_decision_traces(result_id,sequence);
            """)
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(lab_notes)").fetchall()
            }
            if "entity_type" not in columns:
                connection.execute(
                    "ALTER TABLE lab_notes ADD COLUMN entity_type TEXT NOT NULL DEFAULT 'strategy_version'"
                )
            if "entity_id" not in columns:
                connection.execute(
                    "ALTER TABLE lab_notes ADD COLUMN entity_id TEXT NOT NULL DEFAULT ''"
                )
            trace_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(lab_decision_traces)").fetchall()
            }
            if "known_at" not in trace_columns:
                connection.execute("ALTER TABLE lab_decision_traces ADD COLUMN known_at TEXT")

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

def _comparability(result: Mapping[str, object]) -> JsonDict:
    configuration = result.get("configuration")
    config = configuration if isinstance(configuration, Mapping) else {}
    data = result.get("data")
    dataset = data if isinstance(data, Mapping) else {}
    costs = result.get("costs")
    return {
        "dataset_fingerprint": dataset.get("fingerprint", config.get("dataset_fingerprint")),
        "instrument": dataset.get("symbol", config.get("symbol")),
        "timeframe": dataset.get("timeframe", config.get("timeframe")),
        "period": [dataset.get("start"), dataset.get("end")],
        "cashflow": config.get("cashflow"),
        "risk_budget": config.get("risk_budget"),
        "execution_model": config.get("execution_model", config.get("mode")),
        "benchmark": config.get("benchmark"),
        "cost_model": config.get("cost_model", costs),
    }
