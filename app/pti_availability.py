from __future__ import annotations

from typing import Any

from app.db import connection
from app.models import UniverseBuildConfig

PTI_AVAILABILITY_VERSION = "1.0.0"
NOT_TRADING_REASON = "not trading on point-in-time reference session"
OUTSIDE_LIMIT_REASON = "outside maximum-symbol limit"


def _pti_snapshot_for_child_job(job_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.ra_point_in_time_universe_snapshots') AS table_name")
            if not cur.fetchone()["table_name"]:
                conn.rollback()
                return None
            cur.execute(
                """
                SELECT s.id,s.snapshot_date
                FROM ra_point_in_time_universe_snapshots s
                WHERE s.child_job_id=%s
                ORDER BY s.created_at DESC LIMIT 1
                """,
                (job_id,),
            )
            row = cur.fetchone()
        conn.rollback()
    return dict(row) if row else None


def _reference_trade_date(snapshot_date, config: UniverseBuildConfig):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT max((b.bar_ts AT TIME ZONE 'America/New_York')::date) AS trade_date
                FROM rd_bars b
                WHERE b.symbol='SPY' AND b.timeframe=%s AND b.feed=%s AND b.adjustment=%s
                  AND b.session_label='regular'
                  AND (b.bar_ts AT TIME ZONE 'America/New_York')::date < %s
                """,
                (config.timeframe, config.feed, config.adjustment, snapshot_date),
            )
            row = cur.fetchone()
        conn.rollback()
    return row["trade_date"] if row else None


def apply_point_in_time_availability_filter(job_id: str, universe_run_id: str, config: UniverseBuildConfig) -> dict[str, Any] | None:
    """Refill a PTI universe using only securities that traded in the last completed market session.

    Normal universe builds are untouched: if the optional PTI schema is absent,
    or if `job_id` is not linked to a PTI snapshot, this returns None. For PTI
    builds, the underlying liquidity, price, fund and symbol-exclusion methodology
    is preserved. The only additional point-in-time rule is availability: a
    structurally eligible symbol must have a regular-session bar on the last
    completed SPY trading date before T.
    """
    snapshot = _pti_snapshot_for_child_job(job_id)
    if not snapshot:
        return None
    reference_date = _reference_trade_date(snapshot["snapshot_date"], config)
    if reference_date is None:
        raise RuntimeError(f"No completed SPY regular session exists before PTI snapshot {snapshot['snapshot_date']}")

    max_symbols = int(config.maximum_symbols or 2_147_483_647)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH available AS MATERIALIZED (
                    SELECT DISTINCT b.symbol
                    FROM rd_bars b
                    WHERE b.timeframe=%s AND b.feed=%s AND b.adjustment=%s
                      AND b.session_label='regular'
                      AND b.bar_ts >= (%s::date::timestamp AT TIME ZONE 'America/New_York')
                      AND b.bar_ts < (((%s::date + 1)::timestamp) AT TIME ZONE 'America/New_York')
                ), base AS MATERIALIZED (
                    SELECT u.symbol,u.included AS old_included,u.rank_by_liquidity
                    FROM ra_analysis_universe u
                    WHERE u.universe_run_id=%s
                      AND (u.exclusion_reason IS NULL OR u.exclusion_reason=%s)
                ), ranked_available AS MATERIALIZED (
                    SELECT b.symbol,
                           row_number() OVER (ORDER BY b.rank_by_liquidity NULLS LAST,b.symbol)::integer AS available_rank
                    FROM base b JOIN available a USING(symbol)
                ), decisions AS MATERIALIZED (
                    SELECT b.symbol,b.old_included,(a.symbol IS NOT NULL) AS available,r.available_rank,
                           (a.symbol IS NOT NULL AND r.available_rank<=%s) AS new_included
                    FROM base b
                    LEFT JOIN available a USING(symbol)
                    LEFT JOIN ranked_available r USING(symbol)
                ), changed AS (
                    UPDATE ra_analysis_universe u SET
                        included=d.new_included,
                        exclusion_reason=CASE
                            WHEN NOT d.available THEN %s
                            WHEN d.new_included THEN NULL
                            ELSE %s
                        END
                    FROM decisions d
                    WHERE u.universe_run_id=%s AND u.symbol=d.symbol
                    RETURNING d.old_included,d.new_included,d.available
                )
                SELECT count(*) FILTER (WHERE old_included AND NOT new_included AND NOT available)::integer AS removed,
                       count(*) FILTER (WHERE NOT old_included AND new_included)::integer AS refilled,
                       count(*) FILTER (WHERE new_included)::integer AS included
                FROM changed
                """,
                (
                    config.timeframe, config.feed, config.adjustment, reference_date, reference_date,
                    universe_run_id, OUTSIDE_LIMIT_REASON, max_symbols,
                    NOT_TRADING_REASON, OUTSIDE_LIMIT_REASON, universe_run_id,
                ),
            )
            counts = dict(cur.fetchone() or {})
            cur.execute(
                """
                SELECT count(*) AS total_symbols,
                    count(*) FILTER (WHERE included) AS included_symbols,
                    count(*) FILTER (WHERE included AND liquidity_tier='A') AS tier_a_symbols,
                    count(*) FILTER (WHERE included AND liquidity_tier='B') AS tier_b_symbols,
                    count(*) FILTER (WHERE included AND liquidity_tier='C') AS tier_c_symbols,
                    count(*) FILTER (WHERE included AND liquidity_tier='D') AS tier_d_symbols
                FROM ra_analysis_universe WHERE universe_run_id=%s
                """,
                (universe_run_id,),
            )
            summary = dict(cur.fetchone())
            cur.execute(
                """
                UPDATE ra_universe_runs SET total_symbols=%s,included_symbols=%s,
                    tier_a_symbols=%s,tier_b_symbols=%s,tier_c_symbols=%s,tier_d_symbols=%s
                WHERE id=%s
                """,
                (
                    summary["total_symbols"],summary["included_symbols"],summary["tier_a_symbols"],
                    summary["tier_b_symbols"],summary["tier_c_symbols"],summary["tier_d_symbols"],universe_run_id,
                ),
            )
            cur.execute(
                """
                UPDATE ra_point_in_time_universe_snapshots SET
                    availability_reference_date=%s,
                    availability_removed_symbols=%s,
                    availability_refilled_symbols=%s,
                    availability_method_version=%s
                WHERE id=%s
                """,
                (reference_date, counts.get("removed") or 0, counts.get("refilled") or 0, PTI_AVAILABILITY_VERSION, snapshot["id"]),
            )
        conn.commit()

    if summary["included_symbols"] > max_symbols:
        raise RuntimeError("PTI availability filter exceeded the configured maximum-symbol limit")
    return {
        "reference_trade_date": reference_date,
        "removed_symbols": counts.get("removed") or 0,
        "refilled_symbols": counts.get("refilled") or 0,
        "availability_version": PTI_AVAILABILITY_VERSION,
        **summary,
    }
