#!/usr/bin/env python3
"""Provision and inspect QuestDB hourly history. Python 3.11+, standard library only.

Provisioning NEVER enables retention. Destructive retention requires a separate
command, successful validation, and an explicit acknowledgement.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
HOST_TABLES = {"cpu", "disk", "diskio", "kernel", "mem", "processes", "swap", "system"}
NUMERIC = {"BYTE", "SHORT", "INT", "LONG", "FLOAT", "DOUBLE"}
RAW_TYPES = {"float": "DOUBLE", "int": "LONG", "bool": "BOOLEAN", "string": "STRING"}
CUTOFF = "dateadd('M', -3, date_trunc('day', now()))"
LOG = logging.getLogger("downsampling")


def ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class QuestDB:
    def __init__(self, url: str, timeout: float = 30):
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("QuestDB URL must be an HTTP(S) URL")
        self.url = url.rstrip("/")
        self.timeout = timeout
        # Local/private database traffic must not go through a system HTTP proxy.
        self.opener = build_opener(ProxyHandler({}))

    def query(self, sql: str) -> list[dict]:
        request = Request(self.url + "/exec?" + urlencode({"query": sql}))
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"QuestDB HTTP {exc.code}: {exc.read().decode()}") from exc
        if "error" in result:
            raise RuntimeError(f"QuestDB: {result['error']} (SQL: {sql})")
        if "ddl" in result or "dml" in result:
            if result.get("ddl", result.get("dml")) != "OK":
                raise RuntimeError(f"Unexpected mutation response: {result}")
            return []
        if "columns" not in result or "dataset" not in result:
            raise RuntimeError(f"Unexpected QuestDB response: {result}")
        if result.get("count", 0) > len(result["dataset"]):
            raise RuntimeError("QuestDB response was truncated; refusing incomplete metadata")
        columns = [column["name"] for column in result["columns"]]
        return [dict(zip(columns, row)) for row in result["dataset"]]

    def columns(self, name: str) -> list[tuple[str, str]]:
        return [(row["column"], row["type"]) for row in self.query(
            f"SELECT * FROM table_columns({literal(name)})"
        )]


def consumers(root: Path = ROOT) -> dict[str, list[tuple[str, str]]]:
    """Use the actual MQTT parser definitions, including optional fields."""
    result = {}
    for path in sorted((root / "telegraf/status").glob("*.conf")):
        with path.open("rb") as stream:
            config = tomllib.load(stream)
        for consumer in config.get("inputs", {}).get("mqtt_consumer", []):
            for parser in consumer["json_v2"]:
                name = parser.get("measurement_name", consumer["name_override"])
                columns = [("device_id", "SYMBOL"), ("device_type", "SYMBOL")]
                columns += [(tag["path"], "SYMBOL") for tag in parser.get("tag", [])]
                columns += [(field.get("rename", field["path"]), RAW_TYPES[field["type"]])
                            for field in parser.get("field", [])]
                columns.append(("timestamp", "TIMESTAMP"))
                if len({column for column, _ in columns}) != len(columns):
                    raise ValueError(f"Duplicate column in {path}")
                result[name] = columns
    if not result:
        raise RuntimeError("No MQTT consumer definitions found")
    return result


def metric_kind(table: str, column: str, datatype: str) -> str:
    if datatype not in NUMERIC:
        return "last"
    if table == "shelly_emdata":
        return "counter"
    if table == "shelly_switch" and column == "energy_total":
        return "counter"
    if table == "shelly_input" and column in {"counts_total", "counts_xtotal"}:
        return "counter"
    if table == "shelly_system":
        if column == "uptime":
            return "counter"
        if column not in {"ram_size", "ram_free", "ram_minimum_free",
                          "filesystem_size", "filesystem_free"}:
            return "last"
    if table == "shelly_switch" and column.startswith("timer_"):
        return "last"
    if table == "diskio" and column != "iops_in_progress":
        return "counter"
    if table == "kernel":
        if column in {"interrupts", "context_switches", "processes_forked"}:
            return "counter"
        if column == "boot_time":
            return "last"
    if table == "swap" and column in {"in", "out"}:
        return "counter"
    if table == "system" and column in {"uptime", "n_cpus"}:
        return "last"
    # Future fields in known tables default to gauges; review changes before
    # reprovisioning. Existing aggregate schemas are never silently replaced.
    return "gauge"


def aggregate_fields(table: str, columns: list[tuple[str, str]]) -> list[tuple[str, str]]:
    result = [("sample_count", "count()")]
    for column, datatype in columns:
        if column == "timestamp" or datatype == "SYMBOL":
            continue
        kind = metric_kind(table, column, datatype)
        functions = {"gauge": ("avg", "min", "max", "count"),
                     "counter": ("first", "last"), "last": ("last",)}[kind]
        result += [(f"{column}_{function}", f"{function}({ident(column)})")
                   for function in functions]
    return result


def aggregate_sql(table: str, columns: list[tuple[str, str]]) -> str:
    selected = [ident("timestamp")]
    # 9.4.3 can create zero-capacity SYMBOL maps for an aggregate over an
    # existing ILP table (reproduced on the real backup). VARCHAR keys avoid
    # the broken symbol-map writer while preserving every grouping value.
    selected += [f"cast({ident(name)} AS VARCHAR) AS {ident(name)}"
                 for name, datatype in columns if datatype == "SYMBOL"]
    selected += [f"{expression} AS {ident(alias)}"
                 for alias, expression in aggregate_fields(table, columns)]
    return ("SELECT " + ", ".join(selected) + f" FROM {ident(table)} "
            "SAMPLE BY 1h ALIGN TO CALENDAR")


def history_sql(table: str, columns: list[tuple[str, str]]) -> str:
    """One representative value per old hour; detailed stats remain in *_1h."""
    raw, hourly = [], []
    for name, datatype in columns:
        column = ident(name)
        kind = metric_kind(table, name, datatype)
        if name == "timestamp" or datatype == "SYMBOL":
            raw.append(column)
            hourly.append(column)
        elif kind == "gauge":
            raw.append(f"cast({column} AS DOUBLE) AS {column}")
            hourly.append(f"{ident(name + '_avg')} AS {column}")
        else:
            raw.append(column)
            hourly.append(f"{ident(name + '_last')} AS {column}")
    raw += ["'raw' AS resolution", "1L AS sample_count"]
    hourly += ["'1h' AS resolution", ident("sample_count")]
    union = (f"SELECT {', '.join(hourly)} FROM {ident(table + '_1h')} "
             f"WHERE timestamp < {CUTOFF} UNION ALL "
             f"SELECT {', '.join(raw)} FROM {ident(table)} WHERE timestamp >= {CUTOFF}")
    # UNION ALL loses designated-timestamp metadata. A timestamp declaration
    # asserts ascending order; sort the complete union first, never just one
    # branch, so SAMPLE BY / LATEST ON / ASOF JOIN can safely consume the view.
    return f'SELECT * FROM ({union} ORDER BY "timestamp") timestamp("timestamp")'


def source_tables(db: QuestDB) -> dict[str, dict]:
    return {row["table_name"]: row for row in db.query("SELECT * FROM tables()")
            if not row["matView"]}


def selected_tables(db: QuestDB, definitions: dict) -> dict[str, dict]:
    tables = source_tables(db)
    names = set(definitions) | (HOST_TABLES & tables.keys())
    missing = names - tables.keys()
    if missing:
        raise RuntimeError(f"Missing source tables; run apply first: {sorted(missing)}")
    return {name: tables[name] for name in sorted(names)}


def validate_source(name: str, metadata: dict) -> None:
    if (metadata["designatedTimestamp"] != "timestamp"
            or metadata["partitionBy"] != "DAY" or not metadata["walEnabled"]):
        raise RuntimeError(f"{name}: expected WAL, daily partitions, timestamp designation")


def view_status(db: QuestDB) -> dict[str, dict]:
    return {row["view_name"]: row for row in db.query("SELECT * FROM materialized_views()")}


def wait_ready(db: QuestDB, table: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        view = view_status(db).get(table + "_1h")
        wal = db.query(f"SELECT name, suspended FROM wal_tables() "
                       f"WHERE name = {literal(table + '_1h')}")
        if wal and wal[0]["suspended"]:
            raise RuntimeError(f"{table}: hourly WAL suspended; inspect server logs; raw TTL unchanged")
        if view and view["view_status"] == "invalid":
            raise RuntimeError(f"{table}: invalid view: {view['invalidation_reason']}")
        # With concurrent ingestion a scheduled view can be a transaction behind.
        # A completed refresh establishes initial population; validate() additionally
        # compares closed-hour counts against raw data before TTL is allowed.
        if view and view["last_refresh_finish_timestamp"] is not None:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{table}: initial refresh still pending; raw TTL unchanged")
        time.sleep(2)


def apply(db: QuestDB, definitions: dict, timeout: float) -> None:
    tables = source_tables(db)
    for name, expected in definitions.items():
        if name not in tables:
            schema = ", ".join(f"{ident(column)} {datatype}" for column, datatype in expected)
            db.query(f"CREATE TABLE {ident(name)} ({schema}) "
                     'TIMESTAMP("timestamp") PARTITION BY DAY WAL')
            LOG.info("Created source table for configured consumer %s", name)
        else:
            validate_source(name, tables[name])
            actual = dict(db.columns(name))
            missing = [(column, datatype) for column, datatype in expected if column not in actual]
            if missing and tables[name]["ttlValue"]:
                raise RuntimeError(f"{name}: schema changed with retention enabled; manual migration required")
            for column, datatype in missing:
                db.query(f"ALTER TABLE {ident(name)} ADD COLUMN {ident(column)} {datatype}")
                LOG.info("Added absent parser field %s.%s", name, column)
    tables = selected_tables(db, definitions)
    views = view_status(db)
    for name, metadata in tables.items():
        validate_source(name, metadata)
        columns = db.columns(name)
        query = aggregate_sql(name, columns)
        hourly = name + "_1h"
        if hourly in views:
            view = views[hourly]
            normalize = lambda sql: " ".join(sql.split()).lower().rstrip(";")
            if (view["base_table_name"] != name
                    or normalize(view["view_sql"]) != normalize(query)):
                raise RuntimeError(f"{hourly}: definition changed; refusing to replace historical summaries")
            if view["view_status"] != "valid":
                raise RuntimeError(f"{hourly}: invalid; investigate without automatic FULL refresh")
        else:
            if metadata["ttlValue"]:
                raise RuntimeError(f"{name}: retention already enabled but hourly view missing; restore history first")
            db.query(f"CREATE MATERIALIZED VIEW {ident(hourly)} REFRESH EVERY 1h "
                     f"AS ({query}) PARTITION BY MONTH")
            LOG.info("Created %s; waiting for initial backfill", hourly)
        wait_ready(db, name, timeout)
        # History views are disposable queries, not archives; replace on schema changes.
        db.query(f"CREATE OR REPLACE VIEW {ident(name + '_history')} AS ({history_sql(name, columns)})")
        LOG.info("Ready: %s and %s_history (raw TTL not changed)", hourly, name)


def validate(db: QuestDB, definitions: dict) -> list[dict]:
    """Check every retained raw row in closed hours, not just recent samples."""
    tables = selected_tables(db, definitions)
    views = view_status(db)
    suspended = [row["name"] for row in db.query("SELECT name, suspended FROM wal_tables()")
                 if row["suspended"]]
    if suspended:
        raise RuntimeError(f"Suspended WAL tables: {suspended}")
    # Capture once so every query uses an identical boundary even across the hour.
    boundary = db.query("SELECT date_trunc('hour', now()) AS boundary")[0]["boundary"]
    report = []
    for name, metadata in tables.items():
        validate_source(name, metadata)
        view = views.get(name + "_1h")
        if not view or view["view_status"] != "valid" or not view["last_refresh_finish_timestamp"]:
            raise RuntimeError(f"{name}: hourly view missing, invalid, or not populated")
        # Recheck the SQL definitions before enabling any destructive retention.
        expected = " ".join(aggregate_sql(name, db.columns(name)).split()).lower()
        if " ".join(view["view_sql"].split()).lower().rstrip(";") != expected:
            raise RuntimeError(f"{name}: aggregate definition mismatch")
        if metadata["ttlValue"]:
            # Old raw rows no longer exist; validate only the complete retained range.
            oldest = db.query(f"SELECT timestamp FROM {ident(name)} ORDER BY timestamp LIMIT 1")
            start = oldest[0]["timestamp"] if oldest else boundary
            start = db.query(f"SELECT dateadd('h', 1, date_trunc('hour', "
                             f"cast({literal(start)} AS TIMESTAMP))) AS start")[0]["start"]
        else:
            start = "1970-01-01T00:00:00.000000Z"
        where = f"timestamp >= {literal(start)} AND timestamp < {literal(boundary)}"
        raw_count = db.query(f"SELECT count() AS n FROM {ident(name)} WHERE {where}")[0]["n"]
        hourly_count = db.query(f"SELECT sum(sample_count) AS n FROM {ident(name + '_1h')} "
                                f"WHERE {where}")[0]["n"] or 0
        if raw_count != hourly_count:
            raise RuntimeError(f"{name}: closed-hour count mismatch: raw={raw_count}, hourly={hourly_count}; "
                               "wait for refresh or investigate; raw TTL unchanged")
        row = {"table": name, "raw_rows_checked": raw_count, "hourly_samples_checked": hourly_count,
               "view_status": view["view_status"], "refresh_finished": view["last_refresh_finish_timestamp"],
               "transaction_lag": view["base_table_txn"] - view["refresh_base_table_txn"],
               "raw_ttl": f"{metadata['ttlValue']} {metadata['ttlUnit']}"}
        report.append(row)
    return report


def enable_retention(db: QuestDB, definitions: dict, acknowledged: bool) -> list[dict]:
    if not acknowledged:
        raise RuntimeError("Requires --acknowledge-data-deletion after a backup and manual validation review")
    report = validate(db, definitions)  # Validate ALL sources before altering ANY TTL.
    for row in report:
        db.query(f"ALTER TABLE {ident(row['table'])} SET TTL 3 MONTHS")
        LOG.warning("Enabled permanent raw-data expiry for %s: 3 calendar months", row["table"])
    return report


def monitor(db: QuestDB, definitions: dict) -> list[dict]:
    """Lightweight polling: no historical scans and no schema/data mutations."""
    tables = selected_tables(db, definitions)
    views = view_status(db)
    report = []
    for name in tables:
        view = views.get(name + "_1h")
        if not view or view["view_status"] not in {"valid", "refreshing"}:
            raise RuntimeError(f"{name}: hourly view missing or invalid")
        lag = view["base_table_txn"] - view["refresh_base_table_txn"]
        finished = view["last_refresh_finish_timestamp"]
        age = ((datetime.now(timezone.utc) - datetime.fromisoformat(finished)).total_seconds()
               if finished else float("inf"))
        if lag > 0 and age > 7200:
            raise RuntimeError(f"{name}: refresh overdue by more than two hours, lag={lag}")
        report.append({"table": name, "status": view["view_status"], "transaction_lag": lag})
    suspended = [row["name"] for row in db.query("SELECT name, suspended FROM wal_tables()")
                 if row["suspended"]]
    if suspended:
        raise RuntimeError(f"Suspended WAL tables: {suspended}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "apply", "validate", "status", "enable-retention", "watch"])
    parser.add_argument("--url", default=os.environ.get("QUESTDB_URL", "http://127.0.0.1:9000"))
    parser.add_argument("--timeout", type=float, default=600, help="Initial backfill wait per table, seconds")
    parser.add_argument("--interval", type=float, default=300, help="Watch poll interval, seconds")
    parser.add_argument("--acknowledge-data-deletion", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = QuestDB(args.url)
    definitions = consumers()
    try:
        if args.command == "plan":
            tables = source_tables(db)
            for name in sorted(set(definitions) | (HOST_TABLES & tables.keys())):
                columns = db.columns(name) if name in tables else definitions[name]
                print(f"-- {name}: hourly stats, raw TTL 3 MONTHS only after explicit approval")
                print(aggregate_sql(name, columns) + ";\n")
        elif args.command == "apply":
            apply(db, definitions, args.timeout)
        elif args.command == "validate":
            print(json.dumps(validate(db, definitions), indent=2))
        elif args.command == "enable-retention":
            print(json.dumps(enable_retention(db, definitions, args.acknowledge_data_deletion), indent=2))
        elif args.command == "status":
            print(json.dumps(list(view_status(db).values()), indent=2))
        elif args.command == "watch":
            # Monitor only: restarting this service must NEVER rebuild an archive,
            # change a schema, or automatically turn on destructive retention.
            while True:
                try:
                    print(json.dumps(monitor(db, definitions)), flush=True)
                except (RuntimeError, OSError, ValueError) as exc:
                    LOG.error("Hourly history health check failed: %s", exc)
                time.sleep(args.interval)
    except (RuntimeError, OSError, ValueError, TimeoutError, URLError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())