#!/usr/bin/env bash
# Server-side metrics capture for the stress test, started in parallel
# with run_stress.py. Writes to results/<TS>/metrics/.
#
# Captured every 5s for as long as this script runs (Ctrl-C to stop):
#   - docker stats (cpu, mem, net, blockio for prod containers)
#   - pg_stat_activity counts + longest active query
#   - host top + iostat
#
# Usage:
#   bash scripts/loadtest/capture_metrics.sh <results_dir>
#
# results_dir is the matching dir created by run_stress.py
# (printed in its first log line as "Output dir: ...").

set -euo pipefail

RESULTS_DIR="${1:?usage: capture_metrics.sh <results_dir>}"
METRICS_DIR="$RESULTS_DIR/metrics"
mkdir -p "$METRICS_DIR"

# josh-box quirks (per CLAUDE.md): stale docker shim on PATH.
export PATH=/usr/bin:$PATH
unset DOCKER_HOST

PROD_CONTAINERS="docsgpt-aztec-backend-1 docsgpt-aztec-worker-1 docsgpt-aztec-postgres-1 docsgpt-aztec-redis-1 docsgpt-aztec-caddy-1 docsgpt-aztec-cloudflared-1"
PG_CONTAINER="docsgpt-aztec-postgres-1"

echo "[capture] writing to $METRICS_DIR"
echo "[capture] sampling every 5s. Ctrl-C to stop."

# Track child PIDs so trap can kill them cleanly.
PIDS=()

cleanup() {
    echo "[capture] stopping samplers..."
    for pid in "${PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    echo "[capture] done. Files in $METRICS_DIR"
}
trap cleanup EXIT INT TERM

# ── docker stats (one-shot every 5s, append) ─────────────────────────────────
(
    while :; do
        ts=$(date -u +%FT%TZ)
        docker stats --no-stream --format \
            '{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}},{{.NetIO}},{{.BlockIO}},{{.PIDs}}' \
            $PROD_CONTAINERS 2>/dev/null | sed "s/^/$ts,/" >> "$METRICS_DIR/docker_stats.csv"
        sleep 5
    done
) &
PIDS+=($!)

# ── pg_stat_activity counts + longest active query duration ──────────────────
(
    echo "ts,total,active,idle,idle_in_tx,longest_active_s,longest_query" > "$METRICS_DIR/pg_stat_activity.csv"
    while :; do
        ts=$(date -u +%FT%TZ)
        docker exec "$PG_CONTAINER" psql -U docsgpt -d docsgpt -At -F',' -c "
            SELECT
                COUNT(*) FILTER (WHERE 1=1) AS total,
                COUNT(*) FILTER (WHERE state='active') AS active,
                COUNT(*) FILTER (WHERE state='idle') AS idle,
                COUNT(*) FILTER (WHERE state='idle in transaction') AS idle_in_tx,
                COALESCE(EXTRACT(EPOCH FROM MAX(NOW() - query_start) FILTER (WHERE state='active')), 0)::numeric(10,2) AS longest_active_s,
                COALESCE((SELECT LEFT(query, 80) FROM pg_stat_activity WHERE state='active' ORDER BY query_start ASC NULLS LAST LIMIT 1), '') AS longest_query
            FROM pg_stat_activity
            WHERE datname='docsgpt';
        " 2>/dev/null | head -1 | sed "s/^/$ts,/" >> "$METRICS_DIR/pg_stat_activity.csv"
        sleep 5
    done
) &
PIDS+=($!)

# ── host top (CPU/mem) every 5s ──────────────────────────────────────────────
(
    while :; do
        ts=$(date -u +%FT%TZ)
        echo "===== $ts =====" >> "$METRICS_DIR/host_top.txt"
        top -b -n 1 -d 0 -w 200 | head -20 >> "$METRICS_DIR/host_top.txt"
        sleep 5
    done
) &
PIDS+=($!)

# ── iostat for the disk Postgres lives on (5s interval, continuous) ──────────
if command -v iostat >/dev/null 2>&1; then
    iostat -x 5 > "$METRICS_DIR/iostat.txt" &
    PIDS+=($!)
fi

# Wait until killed
wait "${PIDS[@]}"
