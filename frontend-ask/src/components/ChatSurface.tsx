import { useCallback, useEffect, useRef, useState } from "react";
import { streamAnswer, type StreamSource } from "../lib/stream";
import { MessageView, type ChatMessage } from "./Message";
import { SigHeadline } from "./SigHeadline";
import { Disclaimer } from "./Disclaimer";

const STARTERS = [
  "What makes Aztec different from other ZK rollups?",
  "How do I start building with Noir?",
  "How does Aztec governance work?",
  "Explain composable privacy in plain terms.",
];

// Sticky-bottom autoscroll: stay pinned to the bottom while streaming,
// but if the reader scrolls up to read older content, stop following the
// stream until they scroll back down. Only force-scrolls on send (the
// reader just acted, so showing their action is correct UX).
const STICKY_THRESHOLD_PX = 80;

export function ChatSurface() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Whether the message list is "stuck" to the bottom — true while the
  // reader is at (or near) the bottom; flips false the moment they
  // scroll up, and back to true when they scroll back down. Default is
  // true so the very first answer streams into view.
  const stickyRef = useRef<boolean>(true);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickyRef.current = dist <= STICKY_THRESHOLD_PX;
  }, []);

  // Follow new content only while the reader is near the bottom.
  useEffect(() => {
    if (!stickyRef.current) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  // Auto-grow textarea up to a sensible cap.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(200, ta.scrollHeight) + "px";
  }, [input]);

  // Cancel any in-flight stream on unmount.
  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  const send = useCallback(
    (override?: string) => {
      const q = (override ?? input).trim();
      if (!q || busy) return;
      setInput("");
      setBusy(true);
      // The reader just submitted — show their message + the streaming
      // answer regardless of where they were scrolled, then follow the
      // stream from there.
      stickyRef.current = true;

      // Build history from prior turns. We pair user messages with the
      // immediately following bot message; partial / errored turns are
      // skipped so we don't poison rephrase context.
      const history: Array<{ prompt: string; response: string }> = [];
      for (let i = 0; i < messages.length - 1; i++) {
        const a = messages[i];
        const b = messages[i + 1];
        if (a.role === "user" && b.role === "bot" && !b.streaming && !b.error) {
          history.push({ prompt: a.text, response: b.text });
        }
      }

      const userMsg: ChatMessage = { id: Date.now(), role: "user", text: q };
      const botId = Date.now() + 1;
      const botMsg: ChatMessage = {
        id: botId,
        role: "bot",
        text: "",
        sources: [],
        streaming: true,
      };
      setMessages((m) => [...m, userMsg, botMsg]);

      // Capture the controller in closure so callbacks fired after a
      // newer send has started can't clear shared state belonging to
      // the new stream. A stale callback's updateBot() is still safe
      // because it filters by botId, but `setBusy(false)` and
      // `abortRef.current = null` are shared and need this guard.
      const controller = new AbortController();
      abortRef.current?.abort();
      abortRef.current = controller;
      const isCurrent = () => abortRef.current === controller;
      const finishIfCurrent = () => {
        if (!isCurrent()) return;
        setBusy(false);
        abortRef.current = null;
      };

      const updateBot = (patch: Partial<Extract<ChatMessage, { role: "bot" }>>) =>
        setMessages((ms) =>
          ms.map((m) => (m.id === botId && m.role === "bot" ? { ...m, ...patch } : m)),
        );

      streamAnswer(
        { question: q, history, signal: controller.signal },
        {
          onAnswerDelta: (delta) =>
            setMessages((ms) =>
              ms.map((m) =>
                m.id === botId && m.role === "bot" ? { ...m, text: m.text + delta } : m,
              ),
            ),
          onSources: (sources: StreamSource[]) => updateBot({ sources }),
          onEnd: () => {
            updateBot({ streaming: false });
            finishIfCurrent();
          },
          onError: (message) => {
            updateBot({ streaming: false, error: message });
            finishIfCurrent();
          },
        },
      ).catch((err: unknown) => {
        const name = err instanceof Error ? err.name : "";
        if (name === "AbortError") {
          updateBot({ streaming: false });
        } else {
          const msg = err instanceof Error ? err.message : "Network error";
          updateBot({ streaming: false, error: msg });
        }
        finishIfCurrent();
      });
    },
    [busy, input, messages],
  );

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const reset = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setMessages([]);
    setBusy(false);
  };

  const hasMessages = messages.length > 0;

  return (
    <section className={"ask-chat" + (hasMessages ? " ask-chat--active" : "")}>
      <header className="ask-chat__head">
        <span className="ask-chat__head-label">
          <span className="num">01</span> Ask Aztec
        </span>
        <span className="ask-chat__head-status">
          <span className="dot"></span> RAG · Online
        </span>
        <span className="ask-chat__head-actions">
          {hasMessages && (
            <button onClick={reset} title="New conversation" aria-label="Reset">
              ↺
            </button>
          )}
        </span>
      </header>

      {!hasMessages && (
        <div className="ask-chat__empty">
          <h2 className="ask-chat__empty-greet">
            <SigHeadline
              text="What would you like to know?"
              frags={[
                [5, 7],
                [16, 18],
                [23, 25],
              ]}
            />
          </h2>
          <p className="ask-chat__empty-sub">
            Ask anything about Aztec — the privacy network, Noir, the token, governance.
            Answers cite the source documents below.
          </p>
          <div className="ask-prompts">
            {STARTERS.map((p, i) => (
              <button
                key={p}
                type="button"
                className="ask-prompt"
                onClick={() => send(p)}
                disabled={busy}
              >
                <span className="ask-prompt__num">{String(i + 1).padStart(2, "0")}</span>
                <span className="ask-prompt__text">{p}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {hasMessages && (
        <div className="ask-chat__messages" ref={scrollRef} onScroll={onScroll}>
          {messages.map((m) => (
            <MessageView key={m.id} msg={m} />
          ))}
        </div>
      )}

      <div className="ask-chat__input-wrap">
        <div className="ask-chat__input">
          <textarea
            ref={taRef}
            rows={1}
            placeholder={
              hasMessages
                ? "Ask a follow-up…"
                : "Ask anything about Aztec — privacy, Noir, the token…"
            }
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKey}
            disabled={busy}
          />
          <span className="ask-chat__input-meta">
            <kbd>⏎</kbd> send
          </span>
          <button
            type="button"
            className="ask-chat__send"
            onClick={() => send()}
            disabled={busy || !input.trim()}
          >
            Send <span className="mono">→</span>
          </button>
        </div>
        <Disclaimer />
      </div>
    </section>
  );
}
