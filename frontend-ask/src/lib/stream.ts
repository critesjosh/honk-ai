// Minimal SSE client for the DocsGPT-Aztec /stream endpoint.
//
// Backend contract (see application/api/answer/routes/base.py):
//   * POST /stream  →  text/event-stream of `data: <json>\n\n` frames.
//   * Frame types: answer | source | id | end | error | thought |
//     tool_calls | tool_calls_pending | structured_answer | retry.
//   * Inter-frame keepalive lines (": ping\n\n") are emitted on silence.
//
// We only consume answer/source/id/end/error here; everything else is
// intentionally surfaced through `onUnknown` so the caller can log
// gracefully without us guessing semantics.

export type StreamSource = {
  source?: string; // public URL post-_aztec_source_url rewrite
  title?: string;
  text?: string; // truncated excerpt (~100 chars + "...")
};

export type StreamEvents = {
  onAnswerDelta: (delta: string) => void;
  onSources: (sources: StreamSource[]) => void;
  onId?: (conversationId: string) => void;
  onEnd?: () => void;
  onError?: (message: string) => void;
};

export type StreamRequest = {
  question: string;
  // History pairs as (prompt, response) tuples — same shape the Discord
  // bot uses. The `/stream` route already accepts a JSON-encoded string
  // OR a native list (see stream_processor._load_conversation_history).
  history: Array<{ prompt: string; response: string }>;
  signal?: AbortSignal;
};

const API_BASE = (
  import.meta.env.VITE_ASK_AZTEC_API_BASE ?? ""
).replace(/\/$/, "");
const AGENT_KEY = import.meta.env.VITE_ASK_AZTEC_AGENT_KEY ?? "";

if (!AGENT_KEY) {
  // Hard-fail at module init in dev so a misconfigured build is loud
  // instead of producing 401s at runtime. In prod this would fire on
  // first script eval, before render.
  // eslint-disable-next-line no-console
  console.error(
    "VITE_ASK_AZTEC_AGENT_KEY is unset — /stream will return 401. " +
      "Did you build with --build-arg VITE_ASK_AZTEC_AGENT_KEY=...?",
  );
}

export async function streamAnswer(
  req: StreamRequest,
  events: StreamEvents,
): Promise<void> {
  const body = {
    question: req.question,
    // String-encoded list-of-pairs is the shape battle-tested through
    // the Discord bot. The widget plan adopts the same wire form.
    history: JSON.stringify(req.history),
    api_key: AGENT_KEY,
    save_conversation: false,
    isNoneDoc: false,
  };

  const res = await fetch(`${API_BASE}/stream`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal: req.signal,
  });

  // Error shapes are mixed: auth failures come back as SSE `type:error`
  // with the right Content-Type, but check_usage() returns plain JSON
  // with HTTP 429. Surface the most informative message we can.
  if (!res.ok || !res.body) {
    events.onError?.(await readErrorMessage(res));
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";

  // Frames are separated by a blank line. SSE comments (lines starting
  // with ":") are keepalives — drop them.
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    let sep: number;
    while ((sep = buf.indexOf("\n\n")) !== -1) {
      const rawFrame = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const dataLines = rawFrame
        .split("\n")
        .filter((line) => line.startsWith("data: "))
        .map((line) => line.slice(6));
      if (dataLines.length === 0) continue;
      const payload = dataLines.join("\n");
      try {
        const event = JSON.parse(payload);
        dispatch(event, events);
      } catch {
        // Drop malformed frames — keepalives also reach here if the
        // backend ever changes form. Not worth surfacing.
      }
    }
  }

  events.onEnd?.();
}

async function readErrorMessage(res: Response): Promise<string> {
  if (res.status === 429) {
    return "Ask Aztec is over capacity right now. Please try again in a few minutes.";
  }
  if (res.status === 401 || res.status === 403) {
    return "The Ask Aztec service is not authorised. Please refresh the page; if this persists, the team has been notified.";
  }
  // Try JSON {error: "..."} first (the shape /stream emits for usage
  // and validation errors), then a single SSE `data: {...}` frame
  // (the shape `error_stream_generate` produces for 4xx auth errors),
  // then plain text, then a generic fallback.
  try {
    const text = await res.text();
    if (!text) return `Stream failed: HTTP ${res.status}`;
    const candidate = text.startsWith("data: ")
      ? text.slice(6).split("\n", 1)[0]
      : text;
    try {
      const parsed = JSON.parse(candidate);
      if (typeof parsed?.error === "string") return parsed.error;
      if (typeof parsed?.message === "string") return parsed.message;
    } catch {
      /* not JSON — fall through */
    }
    return text.length < 200 ? text : `Stream failed: HTTP ${res.status}`;
  } catch {
    return `Stream failed: HTTP ${res.status}`;
  }
}

function dispatch(event: { type?: string } & Record<string, unknown>, events: StreamEvents): void {
  switch (event.type) {
    case "answer":
      if (typeof event.answer === "string") events.onAnswerDelta(event.answer);
      break;
    case "source":
      if (Array.isArray(event.source)) events.onSources(event.source as StreamSource[]);
      break;
    case "id":
      if (typeof event.id === "string") events.onId?.(event.id);
      break;
    case "end":
      events.onEnd?.();
      break;
    case "error":
      events.onError?.(typeof event.error === "string" ? event.error : "Stream error");
      break;
    default:
      // thought / tool_calls / structured_answer / retry — ignored by
      // the public surface (the public agent has no tools and the
      // canonical Aztec prompt is unstructured).
      break;
  }
}
