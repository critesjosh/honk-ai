#!/usr/bin/env python3
"""Stress harness for the DocsGPT Aztec deployment.

Three client classes target two endpoints:
  widget   -> POST /stream (SSE), save_conversation=false
  discord  -> POST /stream (SSE), history-as-string payload, save_conversation=false
  mcp      -> POST /api/search (JSON, not SSE)

Phases (--phase):
  A    /stream-only ramp (widget+discord), isolates SSE path
  B    /api/search-only ramp (mcp), isolates per-source loop
  C    mixed soak at 75/10/15 widget/discord/mcp
  all  A then B then C, sequential

Each request captures phase-split timings so retrieval slowness can be
distinguished from upstream LLM slowness without server-side hooks:

  retrieval_ms = t_first_source_frame  - t_headers_received
  llm_ttft_ms  = t_first_answer_token  - t_first_source_frame
  total_ms     = t_last_frame          - t_request_start

A background coroutine watches a rolling 60s window and aborts the run
if any SLO stop-trigger from PLAN-stress-test.md §4.5 fires.

Usage:
  pip install -r scripts/loadtest/requirements.txt   # one-time
  python scripts/loadtest/run_stress.py \
      --base-url https://aztec.adjacentpossible.dev \
      --api-key <agent_uuid> \
      --phase all \
      --max-requests 3000

Dry-run against the dev compose first:
  python scripts/loadtest/run_stress.py \
      --base-url http://localhost:7091 \
      --api-key <dev_agent_uuid> \
      --phase A --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import logging
import random
import statistics
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import httpx
except ImportError:
    sys.stderr.write(
        "httpx missing — install with: pip install -r scripts/loadtest/requirements.txt\n"
    )
    sys.exit(2)


LOGGER = logging.getLogger("stress")

# ─────────────────────────────────────────────────────────── SLO thresholds ──
# These mirror PLAN-stress-test.md §0 / §4.5 and trip the stop-event.
SLO_STREAM_P95_TOTAL_S = 25.0
SLO_SEARCH_P95_TOTAL_S = 8.0
SLO_ERROR_RATE = 0.02           # 2 %
SLO_PG_NUMBACKENDS = 95
ROLLING_WINDOW_S = 60.0

# Sampling: hot prompts get this multiplier.
HOT_WEIGHT = 4

# Mixed-phase traffic shares.
MIX_SHARES = {"widget": 0.75, "discord": 0.10, "mcp": 0.15}

# Per-client request timeouts (seconds).
TIMEOUT_STREAM_S = 60.0
TIMEOUT_SEARCH_S = 60.0  # match the real MCP server client timeout


# ──────────────────────────────────────────────────────────────── data class ──
@dataclass
class Result:
    req_id: str
    phase: str
    stage: str
    client: str
    prompt_id: str
    t_request_start: float
    t_headers: float | None = None
    t_first_sse: float | None = None
    t_first_source: float | None = None
    t_first_answer: float | None = None
    t_last_frame: float | None = None
    status: int | None = None
    error: str = ""
    answer_chars: int = 0
    sources_count: int = 0

    @property
    def total_ms(self) -> float | None:
        if self.t_last_frame is None:
            return None
        return (self.t_last_frame - self.t_request_start) * 1000.0

    @property
    def ttft_ms(self) -> float | None:
        """User-perceived first-token latency.

        On the prod backend the SSE producer does not flush headers until
        the first content chunk hits the queue, so `t_headers ≈ first
        content`. (The `{type:"source"}` frame is emitted at end-of-stream,
        not after retrieval, so a client-side retrieval/LLM phase split is
        not extractable.)
        """
        if self.t_headers is None:
            return None
        return (self.t_headers - self.t_request_start) * 1000.0

    @property
    def is_error(self) -> bool:
        if self.error:
            return True
        if self.status is None or self.status >= 400:
            return True
        return False


# ───────────────────────────────────────────────────────────── prompt pool ───
@dataclass
class Prompt:
    id: str
    tag: str
    query: str
    hot: bool = False
    history: list = field(default_factory=list)


def load_prompts(path: Path) -> tuple[list[Prompt], list[Prompt]]:
    """Returns (single_turn_pool, multi_turn_pool)."""
    raw = json.loads(path.read_text())
    single = []
    for p in raw["originals"] + raw["paraphrases"]:
        single.append(Prompt(p["id"], p["tag"], p["query"], hot=p.get("hot", False)))
    multi = [
        Prompt(p["id"], p["tag"], p["query"], history=p.get("history", []))
        for p in raw["multi_turn"]
    ]
    return single, multi


def weighted_pick(prompts: list[Prompt], rng: random.Random) -> Prompt:
    pool = []
    for p in prompts:
        pool.extend([p] * (HOT_WEIGHT if p.hot else 1))
    return rng.choice(pool)


# ──────────────────────────────────────────────────────── SSE line iterator ──
async def iter_sse_data_lines(resp: httpx.Response) -> Iterable[bytes]:
    """Yields each *data:* payload (bytes) from an SSE response.

    Skips comment frames (lines starting with `:`, used here as 15s heartbeat).
    """
    buf = b""
    async for chunk in resp.aiter_bytes():
        buf += chunk
        while b"\n\n" in buf:
            event, _, buf = buf.partition(b"\n\n")
            for line in event.split(b"\n"):
                line = line.rstrip(b"\r")
                if not line or line.startswith(b":"):
                    continue
                if line.startswith(b"data:"):
                    yield line[5:].lstrip()


# ───────────────────────────────────────────────────────── client functions ──
WIDGET_MODEL_ID = "x-ai/grok-4.1-fast"
DISCORD_MODEL_ID = "qwen/qwen3.6-flash"

# Canonical Aztec system prompt used by the prod docs.aztec.network agent
# (verified 2026-04-27: prompts.name = "Aztec 4.3.0 — grounded, Discord-safe",
# ~6010 chars / ~1500 input tokens).
#
# CAVEAT: passing "prompt_id" in the request body is IGNORED by the
# /stream backend when the request is api_key-authenticated — the
# stream_processor resolves prompt_id from the agent row, not the
# request payload. The harness sends it anyway for completeness, but
# if the loadtest agent's row has an empty/wrong prompt_id, the LLM
# input is shorter than real traffic and timing measurements skew
# optimistic. To force a representative system prompt, update the
# loadtest agent in Postgres:
#   UPDATE agents SET prompt_id='0780959b-3c18-4ad9-8284-691665233a6f'
#     WHERE name LIKE 'Aztec MCP - loadtest-bot%';
PROD_PROMPT_ID = "0780959b-3c18-4ad9-8284-691665233a6f"


async def widget_request(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    prompt: Prompt,
    res: Result,
) -> None:
    """POST /stream — widget label.

    Sends `history` as a JSON-encoded string (the legacy/Discord shape).
    The Aztec fork's `_load_conversation_history` accepts both this and a
    native list, so this form works against both the patched prod backend
    and the unpatched upstream-style dev backend. Forces `model_id` to
    grok-4.1-fast so the load mimics the prod widget agent regardless of
    the loadtest agent row's `default_model_id`. The Discord-bot path
    forces `qwen/qwen3.6-flash` — see `DISCORD_MODEL_ID` and
    `discord_request`.
    """
    payload = {
        "question": prompt.query,
        "api_key": api_key,
        "history": json.dumps(prompt.history or []),
        "save_conversation": False,
        "model_id": WIDGET_MODEL_ID,
        "prompt_id": PROD_PROMPT_ID,
    }
    await _consume_stream(client, base_url, payload, res)


async def discord_request(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    prompt: Prompt,
    res: Result,
) -> None:
    """POST /stream — discord label. Forces qwen3.6-flash to mimic the bot."""
    payload = {
        "question": prompt.query,
        "api_key": api_key,
        "history": json.dumps(prompt.history or []),
        "save_conversation": False,
        "model_id": DISCORD_MODEL_ID,
        "prompt_id": PROD_PROMPT_ID,
    }
    await _consume_stream(client, base_url, payload, res)


async def _consume_stream(
    client: httpx.AsyncClient,
    base_url: str,
    payload: dict,
    res: Result,
) -> None:
    """Shared SSE consumer for /stream.

    Recognised frame types:
      - source : retrieval done; sets t_first_source. Not all backends emit.
      - thought, answer, llm_response : LLM content tokens. The first of
        any of these sets t_first_answer (the user-perceived first token).
      - error : server-side error frame.
    """
    res.t_request_start = time.monotonic()
    content_chars = 0
    sources_count = 0
    try:
        async with client.stream(
            "POST",
            f"{base_url}/stream",
            json=payload,
            timeout=TIMEOUT_STREAM_S,
        ) as r:
            res.t_headers = time.monotonic()
            res.status = r.status_code
            if r.status_code >= 400:
                _ = await r.aread()
                res.error = f"http_{r.status_code}"
                res.t_last_frame = time.monotonic()
                return
            async for raw in iter_sse_data_lines(r):
                now = time.monotonic()
                if res.t_first_sse is None:
                    res.t_first_sse = now
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                if t == "source" and res.t_first_source is None:
                    res.t_first_source = now
                    src = obj.get("source") or []
                    sources_count = len(src) if isinstance(src, list) else 0
                elif t in ("thought", "answer", "llm_response"):
                    if res.t_first_answer is None:
                        res.t_first_answer = now
                    chunk = (
                        obj.get("answer")
                        or obj.get("thought")
                        or obj.get("llm_response")
                        or ""
                    )
                    content_chars += len(chunk)
                elif t == "error":
                    res.error = "server_error_frame:" + str(obj)[:120]
            res.t_last_frame = time.monotonic()
            res.answer_chars = content_chars
            res.sources_count = sources_count
    except (httpx.TimeoutException, asyncio.TimeoutError):
        res.error = "client_timeout"
        res.t_last_frame = time.monotonic()
    except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
        res.error = f"disconnect:{type(exc).__name__}"
        res.t_last_frame = time.monotonic()
    except Exception as exc:  # noqa: BLE001 — catchall for harness robustness
        res.error = f"exc:{type(exc).__name__}:{exc!s:.80}"
        res.t_last_frame = time.monotonic()


async def mcp_request(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    prompt: Prompt,
    res: Result,
) -> None:
    """POST /api/search — JSON, not SSE."""
    payload = {
        "question": prompt.query,
        "api_key": api_key,
        "chunks": 8,  # stress more sources than the default 5
    }
    res.t_request_start = time.monotonic()
    try:
        r = await client.post(
            f"{base_url}/api/search",
            json=payload,
            timeout=TIMEOUT_SEARCH_S,
        )
        res.t_headers = time.monotonic()
        res.status = r.status_code
        # /api/search has no SSE phase split — t_first_source ≈ t_last_frame
        res.t_first_source = res.t_headers
        res.t_first_answer = res.t_headers  # no LLM phase on this path
        res.t_last_frame = time.monotonic()
        if r.status_code >= 400:
            res.error = f"http_{r.status_code}"
            return
        try:
            data = r.json()
            if isinstance(data, list):
                res.sources_count = len(data)
                res.answer_chars = sum(len(d.get("text", "") or "") for d in data)
        except Exception:  # noqa: BLE001
            pass
    except (httpx.TimeoutException, asyncio.TimeoutError):
        res.error = "client_timeout"
        res.t_last_frame = time.monotonic()
    except Exception as exc:  # noqa: BLE001
        res.error = f"exc:{type(exc).__name__}:{exc!s:.80}"
        res.t_last_frame = time.monotonic()


CLIENT_FUNCS = {
    "widget": widget_request,
    "discord": discord_request,
    "mcp": mcp_request,
}


# ───────────────────────────────────────────────────────── stop-trigger task ──
class StopController:
    """Tracks rolling 60s metrics and flips an event when SLOs break.

    Kept simple: each stage feeds Result objects in via record(); a background
    monitor loop checks at 5s intervals.
    """

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.reason: str = ""
        self._results: deque[Result] = deque()
        self._req_count = 0

    def record(self, r: Result) -> None:
        if r.t_last_frame is None:
            return
        self._results.append(r)
        self._req_count += 1

    @property
    def total_requests(self) -> int:
        return self._req_count

    def _trim(self, now: float) -> None:
        cutoff = now - ROLLING_WINDOW_S
        while self._results and self._results[0].t_last_frame < cutoff:
            self._results.popleft()

    def _check(self) -> None:
        now = time.monotonic()
        self._trim(now)
        window = list(self._results)
        if len(window) < 5:
            return  # too few samples to judge

        errors = sum(1 for r in window if r.is_error)
        err_rate = errors / len(window)
        if err_rate > SLO_ERROR_RATE:
            self._fire(f"error_rate {err_rate:.1%} over {len(window)} reqs")
            return

        stream_results = [r for r in window if r.client in ("widget", "discord")]
        search_results = [r for r in window if r.client == "mcp"]
        if len(stream_results) >= 5:
            totals = sorted(
                r.total_ms for r in stream_results if r.total_ms is not None
            )
            if totals:
                p95 = totals[max(0, int(0.95 * len(totals)) - 1)]
                if p95 > SLO_STREAM_P95_TOTAL_S * 1000.0:
                    self._fire(f"/stream P95 total {p95:.0f}ms over {len(totals)} reqs")
                    return
        if len(search_results) >= 5:
            totals = sorted(
                r.total_ms for r in search_results if r.total_ms is not None
            )
            if totals:
                p95 = totals[max(0, int(0.95 * len(totals)) - 1)]
                if p95 > SLO_SEARCH_P95_TOTAL_S * 1000.0:
                    self._fire(f"/api/search P95 total {p95:.0f}ms over {len(totals)} reqs")
                    return

    def _fire(self, reason: str) -> None:
        if not self.event.is_set():
            self.reason = reason
            self.event.set()
            LOGGER.error("STOP-TRIGGER: %s", reason)

    async def monitor(self) -> None:
        while not self.event.is_set():
            await asyncio.sleep(5.0)
            self._check()


# ─────────────────────────────────────────────────────────── stage runner ────
@dataclass
class Stage:
    phase: str
    name: str
    concurrency: int
    duration_s: float
    mix: dict[str, float]   # client → share, must sum to 1


async def run_stage(
    stage: Stage,
    base_url: str,
    api_key: str,
    single: list[Prompt],
    multi: list[Prompt],
    rng: random.Random,
    csv_writer: csv.DictWriter,
    csv_lock: asyncio.Lock,
    stop: StopController,
    max_requests: int | None,
) -> dict:
    LOGGER.info(
        "STAGE %s/%s start: %d concurrent for %.0fs, mix=%s",
        stage.phase, stage.name, stage.concurrency, stage.duration_s, stage.mix,
    )
    stage_results: list[Result] = []
    deadline = time.monotonic() + stage.duration_s
    sem = asyncio.Semaphore(stage.concurrency)

    async with httpx.AsyncClient(http2=False) as client:

        async def one_request() -> None:
            client_name = _weighted_choice(stage.mix, rng)
            # 25% chance of multi-turn for stream traffic
            use_multi = client_name in ("widget", "discord") and rng.random() < 0.25
            prompt = rng.choice(multi) if use_multi else weighted_pick(single, rng)
            res = Result(
                req_id=uuid.uuid4().hex[:12],
                phase=stage.phase,
                stage=stage.name,
                client=client_name,
                prompt_id=prompt.id,
                t_request_start=time.monotonic(),
            )
            try:
                await CLIENT_FUNCS[client_name](client, base_url, api_key, prompt, res)
            finally:
                stop.record(res)
                stage_results.append(res)
                async with csv_lock:
                    csv_writer.writerow(_row(res))

        tasks: set[asyncio.Task] = set()
        try:
            while time.monotonic() < deadline and not stop.event.is_set():
                if max_requests is not None and stop.total_requests >= max_requests:
                    LOGGER.warning("max_requests=%d reached", max_requests)
                    break
                await sem.acquire()

                async def _wrapped() -> None:
                    try:
                        await one_request()
                    finally:
                        sem.release()

                t = asyncio.create_task(_wrapped())
                tasks.add(t)
                t.add_done_callback(tasks.discard)
                # tiny pacing jitter to avoid thundering-herd starts
                await asyncio.sleep(0.005)
            # drain
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

    return _stage_summary(stage, stage_results)


def _weighted_choice(mix: dict[str, float], rng: random.Random) -> str:
    r = rng.random()
    cum = 0.0
    for k, w in mix.items():
        cum += w
        if r <= cum:
            return k
    return next(iter(mix))


def _row(r: Result) -> dict:
    return {
        "req_id": r.req_id,
        "phase": r.phase,
        "stage": r.stage,
        "client": r.client,
        "prompt_id": r.prompt_id,
        "status": r.status if r.status is not None else "",
        "error": r.error,
        "answer_chars": r.answer_chars,
        "sources_count": r.sources_count,
        "t_request_start": f"{r.t_request_start:.4f}",
        "t_headers_ms": _delta_ms(r.t_request_start, r.t_headers),
        "t_first_sse_ms": _delta_ms(r.t_request_start, r.t_first_sse),
        "t_first_source_ms": _delta_ms(r.t_request_start, r.t_first_source),
        "t_first_answer_ms": _delta_ms(r.t_request_start, r.t_first_answer),
        "t_last_frame_ms": _delta_ms(r.t_request_start, r.t_last_frame),
        "ttft_ms": _fmt(r.ttft_ms),
        "total_ms": _fmt(r.total_ms),
    }


def _delta_ms(t0: float, t: float | None) -> str:
    if t is None:
        return ""
    return f"{(t - t0) * 1000.0:.1f}"


def _fmt(v: float | None) -> str:
    if v is None:
        return ""
    return f"{v:.1f}"


def _pctile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(p * len(s)) - 1))
    return s[idx]


def _stage_summary(stage: Stage, results: list[Result]) -> dict:
    by_client: dict[str, list[Result]] = {}
    for r in results:
        by_client.setdefault(r.client, []).append(r)

    summary = {
        "phase": stage.phase,
        "stage": stage.name,
        "concurrency": stage.concurrency,
        "duration_s": stage.duration_s,
        "n_requests": len(results),
        "n_errors": sum(1 for r in results if r.is_error),
        "by_client": {},
    }
    for client, rs in by_client.items():
        totals = [r.total_ms for r in rs if r.total_ms is not None]
        ttfts = [r.ttft_ms for r in rs if r.ttft_ms is not None]
        summary["by_client"][client] = {
            "n": len(rs),
            "errors": sum(1 for r in rs if r.is_error),
            "p50_total_ms": round(statistics.median(totals), 1) if totals else None,
            "p95_total_ms": round(_pctile(totals, 0.95) or 0, 1) if totals else None,
            "p50_ttft_ms": round(statistics.median(ttfts), 1) if ttfts else None,
            "p95_ttft_ms": round(_pctile(ttfts, 0.95) or 0, 1) if ttfts else None,
        }
    return summary


# ───────────────────────────────────────────────────────────── stage tables ──
def stages_phase_a() -> list[Stage]:
    """`/stream`-only ramp (widget+discord). Option #4: ceiling-find."""
    mix = {"widget": 0.88, "discord": 0.12, "mcp": 0.0}
    spec = [
        ("step_16", 16, 180),
        ("step_20", 20, 180),
        ("step_24", 24, 180),
        ("step_28", 28, 180),
        ("step_32", 32, 180),
    ]
    return [Stage("A", n, c, d, mix) for n, c, d in spec]


def stages_phase_b() -> list[Stage]:
    """`/api/search`-only ramp (mcp). Option #1: 5 steps."""
    mix = {"widget": 0.0, "discord": 0.0, "mcp": 1.0}
    spec = [
        ("step_2", 2, 120),
        ("step_4", 4, 120),
        ("step_8", 8, 120),
        ("step_12", 12, 120),
        ("step_16", 16, 120),
    ]
    return [Stage("B", n, c, d, mix) for n, c, d in spec]


def stages_phase_c() -> list[Stage]:
    """Mixed soak. Option #4: 10-min soak at c=16."""
    mix = MIX_SHARES
    spec = [
        ("mix_16_soak", 16, 600),
    ]
    return [Stage("C", n, c, d, mix) for n, c, d in spec]


def dry_run_stages() -> list[Stage]:
    """Quick localhost validation: ~90s total, low concurrency."""
    return [
        Stage("DRY", "stream_2", 2, 30, {"widget": 1.0, "discord": 0.0, "mcp": 0.0}),
        Stage("DRY", "search_2", 2, 30, {"widget": 0.0, "discord": 0.0, "mcp": 1.0}),
        Stage("DRY", "mixed_3", 3, 30, MIX_SHARES),
    ]


# ───────────────────────────────────────────────────────────────── main ──────
async def amain(args: argparse.Namespace) -> int:
    if args.no_timestamp:
        out_dir = Path(args.output_dir)
    else:
        out_dir = Path(args.output_dir) / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Output dir: %s", out_dir)

    prompts_path = Path(args.prompts_file)
    single, multi = load_prompts(prompts_path)
    LOGGER.info("Loaded %d single + %d multi prompts", len(single), len(multi))

    # Pick stages
    if args.dry_run:
        stages = dry_run_stages()
    elif args.phase == "A":
        stages = stages_phase_a()
    elif args.phase == "B":
        stages = stages_phase_b()
    elif args.phase == "C":
        stages = stages_phase_c()
    else:
        stages = stages_phase_a() + stages_phase_b() + stages_phase_c()

    # Per-request CSV
    csv_path = out_dir / "requests.csv"
    csv_fields = [
        "req_id", "phase", "stage", "client", "prompt_id",
        "status", "error", "answer_chars", "sources_count",
        "t_request_start",
        "t_headers_ms", "t_first_sse_ms", "t_first_source_ms",
        "t_first_answer_ms", "t_last_frame_ms",
        "ttft_ms", "total_ms",
    ]
    csv_file = csv_path.open("w", newline="")
    csv_writer = csv.DictWriter(csv_file, fieldnames=csv_fields)
    csv_writer.writeheader()
    csv_lock = asyncio.Lock()

    # Stop controller
    stop = StopController()
    monitor_task = asyncio.create_task(stop.monitor())

    rng = random.Random(args.seed)
    summaries: list[dict] = []

    overall_start = time.monotonic()
    try:
        for stage in stages:
            if stop.event.is_set():
                LOGGER.error("Skipping remaining stages: stop fired (%s)", stop.reason)
                break
            if args.max_requests is not None and stop.total_requests >= args.max_requests:
                LOGGER.warning("Global max_requests reached (%d)", args.max_requests)
                break
            stage_summary = await run_stage(
                stage,
                args.base_url,
                args.api_key,
                single,
                multi,
                rng,
                csv_writer,
                csv_lock,
                stop,
                args.max_requests,
            )
            summaries.append(stage_summary)
            (out_dir / f"summary_{stage.phase}_{stage.name}.json").write_text(
                json.dumps(stage_summary, indent=2)
            )
            LOGGER.info(
                "STAGE %s/%s done: %d reqs, %d errors. Stop set=%s",
                stage.phase, stage.name,
                stage_summary["n_requests"], stage_summary["n_errors"],
                stop.event.is_set(),
            )
    finally:
        monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task
        csv_file.close()

    overall = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_s": round(time.monotonic() - overall_start, 1),
        "base_url": args.base_url,
        "phase": args.phase,
        "dry_run": args.dry_run,
        "max_requests": args.max_requests,
        "total_requests": stop.total_requests,
        "stop_fired": stop.event.is_set(),
        "stop_reason": stop.reason,
        "stages": summaries,
    }
    (out_dir / "overall.json").write_text(json.dumps(overall, indent=2))
    LOGGER.info("Done. Total %d requests. Stop fired: %s",
                stop.total_requests, stop.event.is_set())
    if stop.reason:
        LOGGER.info("Stop reason: %s", stop.reason)

    return 0 if not stop.event.is_set() else 1


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", required=True,
                    help="e.g. https://aztec.adjacentpossible.dev or http://localhost:7091")
    ap.add_argument("--api-key", required=True,
                    help="Agent UUID used as api_key in /stream and /api/search payloads.")
    ap.add_argument("--phase", choices=["A", "B", "C", "all"], default="all")
    ap.add_argument("--dry-run", action="store_true",
                    help="Run a short ~90s validation run (overrides --phase).")
    ap.add_argument("--max-requests", type=int, default=3000,
                    help="Hard cap on total requests across all stages (default 3000).")
    ap.add_argument("--prompts-file", default=str(Path(__file__).parent / "prompts.json"))
    ap.add_argument("--output-dir", default=str(Path(__file__).parent / "results"))
    ap.add_argument("--no-timestamp", action="store_true",
                    help="Write directly into --output-dir without a timestamp subfolder.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--verbose", "-v", action="count", default=0)
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
