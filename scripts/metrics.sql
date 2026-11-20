-- Fleet metrics from agentd's run ledger.
--   sqlite3 -header -column ~/.agentd/state.db < scripts/metrics.sql
-- One row per headless run is written to `runs` when the run ends. Attended
-- (interactive) sessions are not metered; only their settle run is.

.print '== totals'
SELECT date(min(started_at), 'unixepoch')        AS first_run,
       date(max(started_at), 'unixepoch')        AS last_run,
       count(*)                                  AS runs,
       count(DISTINCT number)                    AS issues,
       round(sum(seconds) / 3600.0, 1)           AS agent_hours,
       sum(turns)                                AS turns,
       round(sum(cost), 2)                       AS cost_usd
FROM runs;

.print '== outcomes'
SELECT CASE WHEN ok = 0 THEN 'failed'
            WHEN blocked = 1 THEN 'blocked'
            ELSE 'done' END                      AS outcome,
       count(*)                                  AS runs,
       round(100.0 * count(*) / (SELECT count(*) FROM runs), 1) AS pct
FROM runs GROUP BY outcome ORDER BY runs DESC;

.print '== by trigger'
SELECT kind, count(*) AS runs, round(sum(cost), 2) AS cost_usd
FROM runs GROUP BY kind ORDER BY runs DESC;

.print '== by week (Monday start)'
SELECT date(started_at, 'unixepoch', 'weekday 0', '-6 days') AS week_of,
       count(*) AS runs, count(DISTINCT number) AS issues,
       round(sum(seconds) / 3600.0, 1) AS agent_hours,
       round(sum(cost), 2) AS cost_usd
FROM runs GROUP BY week_of;

.print '== by route'
SELECT route, count(*) AS runs, count(DISTINCT number) AS issues,
       round(sum(seconds) / 3600.0, 1) AS agent_hours,
       round(sum(cost), 2) AS cost_usd, round(avg(cost), 2) AS avg_cost,
       sum(blocked) AS blocked, sum(ok = 0) AS failed
FROM runs GROUP BY route ORDER BY runs DESC;

.print '== session reuse (runs per issue+route)'
SELECT runs_in_session, count(*) AS sessions
FROM (SELECT count(*) AS runs_in_session FROM runs GROUP BY number, route)
GROUP BY runs_in_session;

.print '== duration and cost percentiles'
WITH r AS (SELECT seconds, cost,
                  row_number() OVER (ORDER BY seconds) AS rs,
                  row_number() OVER (ORDER BY cost)    AS rc,
                  count(*) OVER ()                     AS n FROM runs)
SELECT (SELECT seconds FROM r WHERE rs = n / 2)            AS median_s,
       (SELECT seconds FROM r WHERE rs = n * 9 / 10)       AS p90_s,
       (SELECT max(seconds) FROM r)                        AS max_s,
       (SELECT round(cost, 2) FROM r WHERE rc = n / 2)     AS median_cost,
       (SELECT round(cost, 2) FROM r WHERE rc = n * 9 / 10) AS p90_cost,
       (SELECT round(max(cost), 2) FROM r)                 AS max_cost;
