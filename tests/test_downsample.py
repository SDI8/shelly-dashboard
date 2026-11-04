"""Unit tests and opt-in integration tests against a DISPOSABLE QuestDB."""

import importlib.util
import os
from pathlib import Path
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("downsample", ROOT / "scripts/downsample.py")
assert SPEC is not None and SPEC.loader is not None
ds = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ds)


class DefinitionTests(unittest.TestCase):
    def test_all_seven_consumers_including_optional_fields(self):
        definitions = ds.consumers()
        self.assertEqual(len(definitions), 7)
        self.assertIn("shelly_input", definitions)
        self.assertIn(("b_total_active_energy", "DOUBLE"), definitions["shelly_emdata"])
        self.assertNotIn(("b_total_act_energy", "DOUBLE"), definitions["shelly_emdata"])
        self.assertIn(("timer_duration", "DOUBLE"), definitions["shelly_switch"])

    def test_counters_are_never_summed_or_averaged(self):
        for table, field in [("shelly_emdata", "b_total_active_energy"),
                             ("shelly_switch", "energy_total"),
                             ("shelly_input", "counts_total"), ("diskio", "read_bytes"),
                             ("kernel", "interrupts"), ("swap", "in")]:
            fields = ds.aggregate_fields(table, [(field, "DOUBLE")])
            self.assertEqual([alias for alias, _ in fields],
                             ["sample_count", field + "_first", field + "_last"])

    def test_gauge_stats_count_values_not_rows(self):
        fields = dict(ds.aggregate_fields("shelly_switch", [("voltage", "DOUBLE")]))
        self.assertEqual(fields["voltage_count"], 'count("voltage")')
        self.assertEqual(fields["sample_count"], "count()")
        self.assertEqual(set(fields), {"sample_count", "voltage_avg", "voltage_min",
                                       "voltage_max", "voltage_count"})

    def test_states_and_metadata_use_last(self):
        self.assertEqual(ds.metric_kind("shelly_system", "config_revision", "LONG"), "last")
        self.assertEqual(ds.metric_kind("shelly_system", "ram_free", "LONG"), "gauge")
        self.assertEqual(ds.metric_kind("shelly_switch", "output", "BOOLEAN"), "last")
        self.assertEqual(ds.metric_kind("shelly_switch", "timer_duration", "DOUBLE"), "last")

    def test_grouping_keeps_every_symbol_dimension(self):
        sql = ds.aggregate_sql("cpu", [("timestamp", "TIMESTAMP"), ("cpu", "SYMBOL"),
                                       ("host", "SYMBOL"), ("usage_user", "DOUBLE")])
        self.assertIn('cast("cpu" AS VARCHAR) AS "cpu"', sql)
        self.assertIn('cast("host" AS VARCHAR) AS "host"', sql)
        self.assertIn("SAMPLE BY 1h ALIGN TO CALENDAR", sql)

    def test_history_has_disjoint_day_aligned_calendar_cutoff(self):
        sql = ds.history_sql("shelly_emdata", [("timestamp", "TIMESTAMP"),
                                             ("b_total_active_energy", "DOUBLE")])
        self.assertIn("timestamp < " + ds.CUTOFF, sql)
        self.assertIn("timestamp >= " + ds.CUTOFF, sql)
        self.assertIn('"b_total_active_energy_last" AS "b_total_active_energy"', sql)
        self.assertNotIn("_avg", sql)
        self.assertIn("'1h' AS resolution", sql)

    def test_identifiers_and_literals_are_escaped(self):
        self.assertEqual(ds.ident('a"b'), '"a""b"')
        self.assertEqual(ds.literal("a'b"), "'a''b'")

    def test_history_restores_designation_after_sorting_complete_union(self):
        sql = ds.history_sql("shelly_temperature", [("timestamp", "TIMESTAMP"),
                                                   ("celsius", "DOUBLE")])
        self.assertTrue(sql.startswith("SELECT * FROM (SELECT "))
        self.assertTrue(sql.endswith('ORDER BY "timestamp") timestamp("timestamp")'))
        self.assertEqual(sql.count("UNION ALL"), 1)
        self.assertGreater(sql.index('ORDER BY "timestamp"'), sql.index("UNION ALL"))

    def test_retention_requires_explicit_acknowledgement(self):
        with self.assertRaisesRegex(RuntimeError, "acknowledge-data-deletion"):
            ds.enable_retention(None, {}, False)

    def test_retention_validates_every_table_before_deleting(self):
        class FakeDB:
            def __init__(self):
                self.queries = []

            def query(self, sql):
                self.queries.append(sql)
                if sql == "SELECT * FROM tables()":
                    return [{"table_name": "shelly_input", "matView": False,
                             "designatedTimestamp": "timestamp", "partitionBy": "DAY",
                             "walEnabled": True, "ttlValue": 0}]
                if "AS boundary" in sql:
                    return [{"boundary": "2026-11-01T10:00:00.000000Z"}]
                return []

        db = FakeDB()
        with self.assertRaises(RuntimeError):
            ds.enable_retention(db, {"shelly_input": []}, True)
        self.assertFalse(any("SET TTL" in sql for sql in db.queries))

    def test_monitor_is_metadata_only_and_accepts_active_refresh(self):
        class FakeDB:
            def __init__(self):
                self.queries = []

            def query(self, sql):
                self.queries.append(sql)
                if sql == "SELECT * FROM tables()":
                    return [{"table_name": "cpu", "matView": False}]
                if sql == "SELECT * FROM materialized_views()":
                    return [{"view_name": "cpu_1h", "view_status": "refreshing",
                             "refresh_base_table_txn": 1, "base_table_txn": 2,
                             "last_refresh_finish_timestamp": ds.datetime.now(ds.timezone.utc).isoformat()}]
                if sql == "SELECT name, suspended FROM wal_tables()":
                    return [{"name": "cpu", "suspended": False}]
                raise AssertionError(f"Unexpected non-metadata query: {sql}")

        db = FakeDB()
        self.assertEqual(ds.monitor(db, {})[0]["status"], "refreshing")
        self.assertEqual(len(db.queries), 3)


@unittest.skipUnless(os.environ.get("QUESTDB_TEST_URL"), "Disposable QuestDB URL not set")
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ["QUESTDB_TEST_URL"]
        if ":19000" not in url:
            raise RuntimeError("Integration tests require a disposable instance on port 19000")
        cls.db = ds.QuestDB(url)
        cls.definitions = ds.consumers()
        # This port is reserved for the explicitly disposable test container.
        for name in sorted(set(cls.definitions) | {"cpu"}):
            cls.db.query(f'DROP VIEW IF EXISTS {ds.ident(name + "_history")}')
            cls.db.query(f'DROP MATERIALIZED VIEW IF EXISTS {ds.ident(name + "_1h")}')
            cls.db.query(f'DROP TABLE IF EXISTS {ds.ident(name)}')

    def test_full_lifecycle(self):
        # Fixtures exercise gauges, NULL values, states, counters, and series keys.
        self.db.query('CREATE TABLE cpu (cpu SYMBOL, usage_user DOUBLE, timestamp TIMESTAMP) '
                      'TIMESTAMP(timestamp) PARTITION BY DAY WAL')
        ds.apply(self.db, self.definitions, 60)
        self.db.query("INSERT INTO shelly_switch (device_id, device_type, id, timestamp, "
                      "active_power, energy_total, output, source, celsius, fahrenheit) VALUES "
                      "('dev1','model','0','2026-01-01T10:05:00Z',10,100,false,'MQTT',20,68),"
                      "('dev1','model','0','2026-01-01T10:35:00Z',30,110,true,'HTTP',22,72),"
                      "('dev1','model','0','2026-01-01T10:45:00Z',null,115,true,'HTTP',22,72),"
                      "('dev1','model','1','2026-01-01T10:05:00Z',99,200,false,'MQTT',20,68)")
        self._wait_rows("shelly_switch", 4)
        self.db.query('REFRESH MATERIALIZED VIEW shelly_switch_1h INCREMENTAL')
        rows = self._wait_rows("shelly_switch_1h", 2)
        row = next(row for row in rows if row["id"] == "0")
        self.assertEqual(row["active_power_avg"], 20)
        self.assertEqual(row["active_power_min"], 10)
        self.assertEqual(row["active_power_max"], 30)
        self.assertEqual(row["active_power_count"], 2)
        self.assertEqual(row["sample_count"], 3)
        self.assertEqual(row["energy_total_first"], 100)
        self.assertEqual(row["energy_total_last"], 115)
        self.assertTrue(row["output_last"])
        self.assertEqual(row["source_last"], "HTTP")
        self.assertEqual(row["timestamp"], "2026-01-01T10:00:00.000000Z")
        # Late arrival in an old bucket must be recomputed, not appended twice.
        self.db.query("INSERT INTO shelly_switch (device_id, device_type, id, timestamp, "
                      "active_power, energy_total) VALUES "
                      "('dev1','model','0','2026-01-01T10:15:00Z',20,105)")
        self._wait_rows("shelly_switch", 5)
        self.db.query('REFRESH MATERIALIZED VIEW shelly_switch_1h INCREMENTAL')
        deadline = time.monotonic() + 30
        while True:
            rows = self.db.query('SELECT * FROM shelly_switch_1h WHERE id = \'0\'')
            if rows and rows[0]["sample_count"] == 4:
                break
            if time.monotonic() > deadline:
                self.fail("Late-data refresh timed out")
            time.sleep(0.2)
        # Idempotence must not drop/rebuild an existing archive.
        ds.apply(self.db, self.definitions, 60)
        ds.validate(self.db, self.definitions)
        history = self.db.query("SELECT * FROM shelly_switch_history WHERE id = '0'")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["resolution"], "1h")
        self.assertEqual(history[0]["active_power"], 20)
        # A plain UNION ALL loses the designation: Grafana's existing SAMPLE BY
        # query must work directly against history without a caller-side wrapper.
        sampled = self.db.query("SELECT timestamp AS Day, sum(active_power)/360 AS Energy "
                    "FROM shelly_switch_history WHERE device_id = 'dev1' "
                    "AND timestamp >= '2026-01-01' AND timestamp < '2026-01-02' "
                    "SAMPLE BY 1d ALIGN TO CALENDAR")
        self.assertEqual(len(sampled), 1)
        self.assertAlmostEqual(sampled[0]["Energy"], (20 + 99) / 360)
        # Current raw rows and old summaries must remain globally chronological.
        self.db.query("INSERT INTO shelly_switch (device_id, device_type, id, timestamp, "
                  "active_power, energy_total) VALUES ('dev1','model','0',now(),40,300)")
        self._wait_rows("shelly_switch", 6)
        mixed = self.db.query("SELECT timestamp, resolution FROM shelly_switch_history "
                      "WHERE device_id = 'dev1'")
        self.assertEqual({row["resolution"] for row in mixed}, {"raw", "1h"})
        self.assertEqual([row["timestamp"] for row in mixed],
                 sorted(row["timestamp"] for row in mixed))
        ds.enable_retention(self.db, self.definitions, True)
        # New timestamp advances source TTL and removes January raw partitions.
        self.db.query("INSERT INTO shelly_switch (device_id, device_type, id, timestamp, "
                      "active_power, energy_total) VALUES ('dev1','model','0',now(),40,300)")
        deadline = time.monotonic() + 30
        while self.db.query("SELECT count() AS n FROM shelly_switch WHERE timestamp < '2026-02-01'")[0]["n"]:
            if time.monotonic() > deadline:
                self.fail("TTL did not expire old raw rows")
            time.sleep(0.2)
        self.db.query('REFRESH MATERIALIZED VIEW shelly_switch_1h INCREMENTAL')
        self._wait_rows("shelly_switch_1h", 3)
        status = ds.view_status(self.db)["shelly_switch_1h"]
        self.assertEqual(status["view_status"], "valid")
        old = self.db.query("SELECT * FROM shelly_switch_1h WHERE timestamp < '2026-02-01'")
        self.assertEqual(len(old), 2, "TTL must not erase old hourly summaries")
        # Repeated provisioning after TTL must preserve archived history.
        ds.apply(self.db, self.definitions, 60)
        self.assertEqual(len(self.db.query("SELECT * FROM shelly_switch_1h "
                                          "WHERE timestamp < '2026-02-01'")), 2)
        ds.validate(self.db, self.definitions)

    def _wait_rows(self, table, count):
        deadline = time.monotonic() + 30
        while True:
            rows = self.db.query(f'SELECT * FROM {ds.ident(table)}')
            if len(rows) == count:
                return rows
            if time.monotonic() > deadline:
                self.fail(f"{table}: expected {count} rows, got {len(rows)}")
            time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()