# Database operations

## Active storage

Docker Compose stores QuestDB in the `questdb_data` named volume (normally
`shelly-dashboard_questdb_data`), mounted at `/var/lib/questdb`. The ignored
`questdb/data` directory in this checkout is not the active database mount.

## Resource limits and restart investigation

On 1 November 2026, the running QuestDB 9.2.2 container had a 1 GiB memory
limit and a one-CPU quota. Its cgroup reported a 1 GiB memory peak and 823
memory-limit hits. Docker retained an unexpected exit event at
19:59:46 UTC with exit code 137 (SIGKILL), followed by an automatic restart.
Neither the inspected cgroup counters nor Docker's inspected state reported
an OOM kill. No fatal JVM error or kernel OOM evidence was found in the
available logs. Memory pressure is established; the source of the SIGKILL
is not established.

Startup logs also showed 16 workers in each shared network, query, and write
pool, plus three WAL-apply and three materialized-view-refresh workers,
despite the one-CPU quota. Compose now grants QuestDB 2 GiB and explicitly
limits each of those five pools to one worker. These setting names were
checked against the deployed 9.2.2 release, and the effective limits and
worker counts were verified after recreating the container.

SQL and WAL metadata checks passed after the change; no WAL table was
suspended. These checks are not a long-term stability guarantee. Avoid using
unbounded production queries as load tests. Check cgroup `memory.events`,
`memory.current`, and `memory.peak`, Docker restart events, and server logs
when investigating recurrence. Exit code 137 alone does not prove an OOM kill.

## Phase-B energy column migration

The Telegraf field now maps Shelly's JSON key `b_total_act_energy` to
`b_total_active_energy`, consistently with phases A and C.

For an existing database:

1. Inspect `table_columns('shelly_emdata')`.
2. Stop Telegraf before changing the database column to prevent the old
   writer from recreating it. MQTT uses QoS 0, so messages during this short
   maintenance window may be lost.
3. If only the old column exists, execute
   [the rename migration](../sql/001-rename-phase-b-energy.sql).
4. Verify the new column exists and the old column is absent, then start
   Telegraf with the updated configuration.
5. Update any external Grafana queries that reference the old column name.

Do not rerun the rename after it succeeds. Fresh installations need no SQL
migration. If both column names already exist, do not drop either: reconcile
their historical data separately before changing the schema.

## Hourly downsampling

### Policy and coverage

- Raw data: **three calendar months**, using daily partition TTL.
- Hourly materialized views: **indefinite** retention, monthly partitions,
   UTC-aligned one-hour buckets, checked for updates every hour.
- All seven MQTT consumers are read from the actual Telegraf parser files.
   Missing consumer tables and optional parser columns are created before
   aggregation, so a currently unused consumer is covered when it first reports.
- The eight host metric tables loaded by Telegraf's image-default configuration
   are also covered when present: `cpu`, `disk`, `diskio`, `kernel`, `mem`,
   `processes`, `swap`, and `system`. Run provisioning again after a new host
   table appears; do not enable retention on an unprovisioned table.
- Every SYMBOL column is preserved as a grouping dimension, including device
   and component IDs and host dimensions where present. Hourly grouping keys
   are stored as VARCHAR rather than SYMBOL; their values are unchanged.

For each raw table `name`, provisioning creates:

| Object | Purpose |
| --- | --- |
| `name` | Original ingestion table, never renamed |
| `name_1h` | Stored hourly summaries |
| `name_history` | Regular SQL view combining old hourly values and recent raw rows |

Regular views require the upgrade from QuestDB 9.2.2. The complete lifecycle
was tested against **9.4.3** before upgrading the live volume, including late
data and confirmation that raw TTL does not invalidate the materialized view
or remove older summaries. No major-version upgrade is required.

The live upgrade was preceded by an offline archive of the complete named
volume at [backups/questdb-before-9.4.3.tar.gz](../backups/questdb-before-9.4.3.tar.gz).
The archive passed both gzip integrity and tar-listing checks. Its SHA-256 is
`160d5157f1a0d6517a19846d2856dc5325114ee6edff267f8b559cf76c8c429f`.
The archive was also restored into an isolated volume and successfully opened
with 9.4.3. It is a local rollback copy, not an off-host backup.
Ingestion was stopped during the offline backup;
QoS-0 MQTT messages during that maintenance window may have been lost.

### Observed 9.4.3 symbol-map failure and workaround

On the real switch-table schema, a new materialized view with native SYMBOL
grouping keys reproducibly created zero-capacity symbol maps. Opening its WAL
writer then failed `SymbolMapWriter`'s `symbolCapacity > 0` assertion. The
view remained `refreshing` with a suspended WAL and no applied transactions;
the raw source remained healthy. The small synthetic fixture did not expose
this failure, but the restored full backup reproduced it.

Casting grouping keys to VARCHAR in the aggregate query succeeded on that
same restored source. All generated hourly views use this workaround. It
preserves series identity and avoids the defective symbol-map creation path;
Grafana sees textual keys in either case. Initial-backfill polling also checks
WAL suspension explicitly and fails promptly without enabling source TTL.
No assertion is disabled and no source table files are edited.

### Live rollout verification

After deploying the VARCHAR-key workaround:

- All **15** source tables have valid hourly materialized views and regular
   history views. The unused `shelly_input` source is empty but provisioned.
- Closed-hour validation accounted for **5,016,545** retained raw rows, exactly
   matching summed hourly `sample_count` values across all sources.
- One closed-hour bucket per populated source was independently recomputed
   from raw rows: all **533** selected summary fields matched (floating-point
   comparisons used a tolerance of `1e-9`).
- The **11-test** suite passed against a disposable 9.4.3 database, including
   TTL preservation, late data, repeat provisioning, and metadata-only monitoring.
- The Compose-based provisioner also passed validation, and its read-only
   monitor was started. QuestDB reported healthy, no unexpected restarts, and
   no cgroup OOM events after the upgrade.

Following manual review, the user approved retention activation on
**4 November 2026**. The guarded acknowledgement command enabled
`TTL 3 MONTHS` on all **15** raw sources after validating matching closed-hour
raw and hourly sample counts. An initial refresh lag blocked activation;
incremental refreshes resolved it without rebuilding any archive. The existing
pre-upgrade backup passed its gzip integrity check before activation.

Post-activation metadata checks confirmed all 15 raw TTL settings, valid hourly
archives with `TTL 0 HOURS` (indefinite retention), and no suspended WAL tables.
The history views and read-only monitor remain deployed. Expiry removes whole
eligible raw partitions as data advances, not necessarily immediately.

### Metric semantics

| Kind | Stored hourly fields |
| --- | --- |
| Numeric gauges | `_avg`, `_min`, `_max`, `_count` (non-null value count) |
| Cumulative counters | `_first`, `_last` |
| Booleans, text, timestamps/revisions and other metadata | `_last` |
| Every bucket | `sample_count` (number of raw rows) |

Energy totals, input pulse counters, disk I/O counters, kernel cumulative
counters, and swap-in/out counters are never summed or averaged. Configuration
revisions and timer metadata use last-value semantics. RAM, power, temperature,
voltage, CPU usage, process counts and filesystem usage are gauges.

Gauge averages are **sample-weighted**, not time-weighted. Counter boundary
readings do not capture resets inside an hour, and `last - first` alone does
not include the interval from the preceding hour's final sample. Derive energy
usage from adjacent readings with reset/gap handling. Last boolean values are
not duty cycles and do not preserve every transition. Partial input rows can
produce null boundary/last values; gauge `_count` distinguishes missing values.

The history views expose original column names, plus `resolution` (`raw` or
`1h`) and `sample_count`. Older numeric gauges use their hourly average;
counters and states use the last reading. Gauge values are exposed as DOUBLE
on both sides, even when the raw field is an integer. The cutoff is midnight
UTC three calendar months ago, deliberately aligned with daily raw partitions.
The branches are disjoint (`< cutoff` and `>= cutoff`), avoiding overlaps or
half-hour gaps. The original source tables still contain full raw history until
TTL is explicitly enabled, even though history views already use old summaries.

`UNION ALL` does not preserve QuestDB's designated-timestamp metadata. Each
history view therefore wraps the complete union in an ascending
`ORDER BY "timestamp"` subquery and applies `timestamp("timestamp")` to that
subquery. Sorting the whole union is required before asserting timestamp order;
sorting just one branch is insufficient. This restores direct `SAMPLE BY`
support for Grafana queries, although the sort can add query cost.

### Provisioning and verification

The implementation is [scripts/downsample.py](../scripts/downsample.py), using
Python 3.11+ and the standard library only. The optional Compose service uses
Python 3.13 and the same read-only repository configuration.

1. Make and verify a database backup before upgrading or enabling deletion.
2. Run `docker compose --profile downsampling run --rm downsampling plan`
    to inspect generated aggregate SQL without changing the database.
3. Run `docker compose --profile downsampling run --rm downsampling apply`.
    This provisions and backfills **one table at a time**. It does not enable
    TTL. Each backfill may take time; the default wait limit is ten minutes per
    table. A timeout does not cancel the database refresh or enable deletion.
4. Run `docker compose --profile downsampling run --rm downsampling validate`
    and manually inspect the results and sample values in the web console.
    Validation compares closed-hour raw row counts against summed hourly sample
    counts, checks aggregate SQL definitions and view validity, and rejects
    suspended WAL tables. It does not prove every individual value is correct;
    the integration suite checks aggregate-value semantics separately.
5. After explicit manual approval, run
    `docker compose --profile downsampling run --rm downsampling enable-retention --acknowledge-data-deletion`.
    This validates **all tables before changing any TTL**. It permanently enables
    `TTL 3 MONTHS` on the source tables. DDL across different tables is not atomic;
    check TTL metadata if interrupted. Re-running this command is safe.
6. Start the lightweight monitor using
    `docker compose --profile downsampling up -d downsampling`.
    It polls only metadata every five minutes and logs missing/invalid views,
    suspended WAL, or refresh lag lasting more than two hours. It never performs
    schema changes, full refreshes, historical scans, or retention activation.

All commands can also run locally as `python scripts/downsample.py COMMAND`.
Use `--url` or `QUESTDB_URL` to select an endpoint. HTTP errors, SQL errors,
unexpected/truncated responses, definition drift, invalid views, and missing
tables fail closed. Existing aggregate definitions are **never automatically
replaced**; this protects retained history after raw expiry. `apply` is repeatable
when definitions have not changed. Review new fields and counter classifications
before reprovisioning.

### Important limitations and recovery

- TTL is data-driven and partition-granular: it removes whole expired partitions
   as data advances, not individual rows precisely at their three-month birthday.
   A quiet table can retain older raw rows longer. Do not promise exact wall-clock
   deletion times or shrink partitions by manually deleting files.
- Native TTL does **not** wait for a failed or lagging materialized view. The
   monitor reports failure; it is not an automatic deletion interlock. Respond
   before a prolonged failure reaches the retention boundary. Keep backups.
- If necessary, stop future expiry with `ALTER TABLE name SET TTL 0 HOURS`.
   This does not restore already deleted rows.
- **Never automatically run `REFRESH MATERIALIZED VIEW ... FULL` after raw
   expiry.** It discards the old archive and rebuilds only from surviving raw
   rows. Likewise, do not drop/recreate the archive, rename its source, or
   manually drop source partitions as routine maintenance.
- Do not insert historical rows older than the retained raw range without a
   recovery plan: recomputing such buckets from incomplete raw data can replace
   complete historical summaries with partial results. The current MQTT parsers
   timestamp on receipt, so normal ingestion does not backdate old device events.
- Schema changes affecting an archived field need a deliberate migration and
   backup. The provisioner refuses incompatible view definitions and refuses
   to create an archive from an already-expiring source.
- Backups contain sensitive device data and configuration. The local backup
   directory is ignored by Git; copy verified backups to separate storage.
- For an upgrade rollback, stop writers and restore the **entire pre-upgrade
   backup into a new volume**, then run the old image against that restored
   volume. Do not simply point an old binary at the upgraded live volume.

### Grafana query examples

Use `shelly_em_history` instead of `shelly_em` for mixed-resolution history:

```sql
SELECT timestamp AS time, device_id, total_active_power, resolution
FROM shelly_em_history
WHERE $__timeFilter(timestamp)
ORDER BY timestamp;
```

Use the materialized view directly for envelopes and counts:

```sql
SELECT timestamp AS time, device_id,
          total_active_power_avg, total_active_power_min, total_active_power_max,
          total_active_power_count
FROM shelly_em_1h
WHERE $__timeFilter(timestamp)
ORDER BY timestamp;
```

When combining several hourly averages, weight each by its **field-specific**
`_count`, not `sample_count`. Do not average raw points and hourly averages
together without accounting for the differing resolution and weights.

Existing queries using `SAMPLE BY 1d ALIGN TO CALENDAR` can query the history
views directly; no caller-side timestamp wrapper is needed.

**Energy warning:** `sum(active_power) / 360` is not a valid general energy
calculation over mixed-resolution history. Old rows are hourly power averages,
whereas recent rows are individual raw readings; a single divisor cannot treat
both equivalently. MQTT readings are also event-driven, not guaranteed evenly
spaced. Prefer differences between cumulative energy readings with explicit
reset/gap handling, or time integration of power with a documented interpolation
policy. Sample counts alone do not provide elapsed-time weights. Restoring
timestamp designation fixes query execution, not this energy-calculation issue.

### Tests

Run `python -m unittest discover -s tests -v` for unit tests. Integration tests
require `QUESTDB_TEST_URL=http://127.0.0.1:19000` pointing at a **disposable**
QuestDB 9.4.3 instance. They reset their fixture tables and enable destructive
TTL there; never point them at production. Tests cover all parser definitions,
optional fields, grouping keys, metric semantics, NULL counts, late data,
repeat provisioning, mixed-history results, global timestamp ordering, direct
`SAMPLE BY` queries against history views, and preservation of old hourly data
after source TTL expiry.