#!/usr/bin/env bash
# External synthetic health check for the DocsGPT-aztec prod stack.
#
# WHY: the 2026-07 outage was SILENT for ~3 days — nothing paged. Run this from
# OUTSIDE josh-box (a bastion cron, or a third-party uptime monitor that can shell
# out) so a downed origin, tunnel, or backend is detected regardless of the box's
# own state. Exits 0 if healthy, non-zero (with a reason on stderr) otherwise, so
# a cron/monitor can alert on the exit code.
#
# Two layers:
#   1. /api/health  — is the backend up at all (cheap liveness).
#   2. /stream      — a synthetic RAG query, to catch "health 200 but RAG broken"
#                     (empty answers, retrieval down). Only runs if STREAM_API_KEY
#                     is set. Do NOT monitor "/" — Caddy returns a static 200
#                     sentinel there even when the backend is down.
#
# Reaching the origin (pick per your topology, since CF Access gates the public
# edge):
#   A) Bastion → tunnel origin (matches AZTEC_SETUP.md's validated probe):
#        HEALTH_URL=http://localhost:5080/api/health HOST_HEADER=$PUBLIC_HOSTNAME \
#          scripts/ops/healthcheck.sh
#   B) Public edge with a Cloudflare Access service token:
#        HEALTH_URL=https://$PUBLIC_HOSTNAME/api/health \
#        CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=... scripts/ops/healthcheck.sh
#
# Env:
#   HEALTH_URL           health endpoint (default https://$PUBLIC_HOSTNAME/api/health)
#   STREAM_URL           /stream endpoint (default: HEALTH_URL with /api/health -> /stream)
#   HOST_HEADER          optional Host header (for the bastion→origin path)
#   CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET   optional CF Access service token
#   STREAM_API_KEY       agent key; if set, run the synthetic /stream check
#   TIMEOUT              per-request timeout seconds (default 20)
set -euo pipefail

PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME:-aztec.adjacentpossible.dev}"
HEALTH_URL="${HEALTH_URL:-https://${PUBLIC_HOSTNAME}/api/health}"
STREAM_URL="${STREAM_URL:-${HEALTH_URL/\/api\/health//stream}}"
TIMEOUT="${TIMEOUT:-20}"

hdrs=()
[ -n "${HOST_HEADER:-}" ] && hdrs+=(-H "Host: ${HOST_HEADER}")
[ -n "${CF_ACCESS_CLIENT_ID:-}" ] && hdrs+=(-H "CF-Access-Client-Id: ${CF_ACCESS_CLIENT_ID}")
[ -n "${CF_ACCESS_CLIENT_SECRET:-}" ] && hdrs+=(-H "CF-Access-Client-Secret: ${CF_ACCESS_CLIENT_SECRET}")

fail() { echo "HEALTHCHECK FAIL: $*" >&2; exit 1; }

# 1) Liveness
body="$(curl -fsS -m "${TIMEOUT}" "${hdrs[@]}" "${HEALTH_URL}" 2>/dev/null)" \
  || fail "GET ${HEALTH_URL} did not return 2xx within ${TIMEOUT}s"
case "${body}" in
  *'"status"'*'"ok"'*) : ;;
  *) fail "GET ${HEALTH_URL} returned unexpected body: ${body}" ;;
esac
echo "OK: backend liveness (${HEALTH_URL})"

# 2) Synthetic RAG (optional)
if [ -n "${STREAM_API_KEY:-}" ]; then
  resp="$(curl -fsS -m "$((TIMEOUT + 15))" "${hdrs[@]}" \
    -H 'Content-Type: application/json' \
    -X POST "${STREAM_URL}" \
    -d "{\"question\":\"What is Aztec?\",\"api_key\":\"${STREAM_API_KEY}\",\"history\":[]}" 2>/dev/null)" \
    || fail "POST ${STREAM_URL} (synthetic /stream) failed within timeout"
  # Robust RAG signal. An error frame is a hard fail; otherwise require an actual
  # answer frame ({"type": "answer", "answer": ...}). A broken RAG path
  # (empty_response, retrieval/LLM failure) emits no answer frame — a bare
  # "data:" line (e.g. a source or error frame) must NOT count as healthy.
  case "${resp}" in
    *'"type": "error"'*|*'"type":"error"'*) fail "synthetic /stream returned an error frame: ${resp:0:200}" ;;
  esac
  case "${resp}" in
    *'"type": "answer"'*|*'"type":"answer"'*) echo "OK: synthetic /stream returned an answer frame" ;;
    *) fail "synthetic /stream produced no answer frame (RAG may be broken): ${resp:0:200}" ;;
  esac
else
  echo "SKIP: synthetic /stream (set STREAM_API_KEY to enable)"
fi

echo "HEALTHCHECK OK"
