import { useCallback, useEffect, useRef, useState } from "react";
import { streamAnswer, type StreamSource } from "../lib/stream";
import { MessageView, type ChatMessage } from "./Message";
import { SigHeadline } from "./SigHeadline";
import { Disclaimer } from "./Disclaimer";
import {
  buildShareUrl,
  clearShareHash,
  copyToClipboard,
  decodeShare,
  encodeShare,
  readShareHash,
  type ShareMessage,
} from "../lib/share";

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

type BotMessage = Extract<ChatMessage, { role: "bot" }>;
type BotPatch = Partial<BotMessage> | ((m: BotMessage) => Partial<BotMessage>);

export function ChatSurface() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [shareState, setShareState] = useState<"idle" | "ok" | "fail">("idle");
  // Screen-reader announcement, rendered into a visually-hidden
  // aria-live="polite" region. Set on successful settle only — errors
  // are announced by the role="alert" paragraph in MessageView, and the
  // token stream itself is deliberately NOT live (per-token chatter).
  const [announcement, setAnnouncement] = useState("");
  // True while the surface is showing a conversation loaded from a
  // `#share=...` URL — used to render a small banner so the recipient
  // knows what they're looking at. Cleared on `reset()` or after the
  // user submits a new question (their thread, their show).
  const [viewingShared, setViewingShared] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Whether the message list is "stuck" to the bottom — true while the
  // reader is at (or near) the bottom; flips false the moment they
  // scroll up, and back to true when they scroll back down. Default is
  // true so the very first answer streams into view.
  const stickyRef = useRef<boolean>(true);
  // Flipped as soon as the user does anything local (sends, resets,
  // edits the textarea). A slow `decodeShare` promise resolving later
  // would otherwise clobber whatever they've started typing or asked.
  const userInteractedRef = useRef<boolean>(false);

  // On first mount, replay a shared conversation if the URL hash carries
  // one. Decoding is async (uses DecompressionStream), so we set state
  // when it resolves; a malformed/oversize hash silently drops back to
  // the empty-state surface.
  useEffect(() => {
    const token = readShareHash();
    if (!token) return;
    let cancelled = false;
    decodeShare(token).then((shared) => {
      if (cancelled || userInteractedRef.current) return;
      if (!shared || shared.length === 0) return;
      const replayed: ChatMessage[] = shared.map((m: ShareMessage, i) =>
        m.role === "user"
          ? { id: i, role: "user", text: m.text }
          : {
              id: i,
              role: "bot",
              text: m.text,
              sources: m.sources ?? [],
              streaming: false,
            },
      );
      setMessages(replayed);
      setViewingShared(true);
    });
    return () => {
      cancelled = true;
    };
  }, []);

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

  // Auto-grow textarea to fit content. CSS `max-height` caps it at 10 lines;
  // past that, overflow-y kicks in and the user scrolls through their message.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + "px";
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
      userInteractedRef.current = true;
      setInput("");
      setBusy(true);
      // Clear the live region so a later identical announcement (two
      // settles in a row) is re-announced by screen readers.
      setAnnouncement("");
      // Once the recipient continues the conversation it's no longer
      // "the shared view"; drop the banner + scrub the hash so an
      // accidental refresh doesn't reload the old transcript over the
      // new one.
      if (viewingShared) {
        setViewingShared(false);
        clearShareHash();
      }
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

      // Accepts either a plain patch or a function of the current bot
      // message, so delta appends can derive from the latest state.
      const updateBot = (patch: BotPatch) =>
        setMessages((ms) =>
          ms.map((m) =>
            m.id === botId && m.role === "bot"
              ? { ...m, ...(typeof patch === "function" ? patch(m) : patch) }
              : m,
          ),
        );

      streamAnswer(
        { question: q, history, signal: controller.signal },
        {
          onAnswerDelta: (delta) => updateBot((m) => ({ text: m.text + delta })),
          onSources: (sources: StreamSource[]) => updateBot({ sources }),
          onEnd: () => {
            updateBot({ streaming: false });
            if (isCurrent()) setAnnouncement("Answer complete");
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

  // Retry a failed turn: the input was cleared at send, so the question
  // only survives in the failed user message. The user+bot pair is
  // adjacent by construction (`send` appends them together); drop both
  // and re-send the user text. `send` builds history from the closure's
  // `messages`, which still contains the errored pair — harmless, since
  // history-building already skips errored turns — and appends the new
  // pair via a functional update, so it lands on the filtered list.
  //
  // `retry` is handed to every memoized MessageView, so its identity
  // must never change — a per-render closure (it reads `messages`,
  // `busy`, `send`) would invalidate the memo and re-render every
  // settled message on each streamed token. Latest-ref pattern: the
  // effect refreshes the implementation after every render, the stable
  // wrapper is what MessageView sees.
  const retryImplRef = useRef<(botId: number) => void>(() => {});
  useEffect(() => {
    retryImplRef.current = (botId: number) => {
      if (busy) return;
      const idx = messages.findIndex((m) => m.id === botId);
      if (idx < 1) return;
      const failed = messages[idx];
      const paired = messages[idx - 1];
      if (failed.role !== "bot" || !failed.error || paired.role !== "user") return;
      setMessages((ms) => ms.filter((m) => m.id !== failed.id && m.id !== paired.id));
      send(paired.text);
    };
  });
  const retry = useCallback((botId: number) => retryImplRef.current(botId), []);

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const reset = () => {
    userInteractedRef.current = true;
    abortRef.current?.abort();
    abortRef.current = null;
    setMessages([]);
    setBusy(false);
    if (viewingShared) {
      setViewingShared(false);
      clearShareHash();
    }
  };

  // Build a shareable URL with the current conversation encoded into the
  // hash and copy it to the clipboard. The encoded blob never leaves the
  // browser; the recipient's browser decompresses it locally.
  //
  // We only serialize complete user+bot pairs. If the latest bot reply is
  // still streaming or errored, dropping just the dangling bot would
  // leave a question with no answer for the recipient — so we drop the
  // trailing user turn too. The Share button is also hidden while
  // `busy`, but a settle could happen between predicate and click.
  const onShare = useCallback(async () => {
    const shareable: ShareMessage[] = [];
    for (let i = 0; i < messages.length; i++) {
      const m = messages[i];
      if (m.role === "user") {
        const next = messages[i + 1];
        if (
          next &&
          next.role === "bot" &&
          !next.streaming &&
          !next.error &&
          next.text.length > 0
        ) {
          shareable.push({ role: "user", text: m.text });
          shareable.push({ role: "bot", text: next.text, sources: next.sources });
          i++;
        }
      }
    }
    if (shareable.length === 0) return;
    try {
      const token = await encodeShare(shareable);
      const url = buildShareUrl(token);
      let copied = await copyToClipboard(url);
      if (!copied) {
        // Last resort — surface the URL through a prompt so the user
        // can still grab it on browsers without clipboard permission.
        // `window.prompt` returns null when the user cancels — don't
        // report that as a successful copy.
        const result = window.prompt("Copy this share link:", url);
        copied = result !== null;
      }
      setShareState(copied ? "ok" : "fail");
    } catch {
      setShareState("fail");
    }
    window.setTimeout(() => setShareState("idle"), 2200);
  }, [messages]);

  const hasMessages = messages.length > 0;
  // Hidden while a stream is in flight — otherwise a click during a
  // follow-up's stream would serialize the dangling user turn (because
  // its bot reply is still `streaming`). Predicate matches `onShare`'s
  // pair-only filter: at least one complete user→bot adjacency.
  const canShare =
    !busy &&
    messages.some((m, i) => {
      const next = messages[i + 1];
      return (
        m.role === "user" &&
        next?.role === "bot" &&
        !next.streaming &&
        !next.error &&
        next.text.length > 0
      );
    });
  const shareLabel =
    shareState === "ok" ? "Link copied" : shareState === "fail" ? "Share failed" : "Share";

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
          {canShare && (
            <button
              onClick={onShare}
              title="Copy a shareable link to this conversation"
              aria-label="Copy share link"
              className={
                "ask-chat__head-share" +
                (shareState === "ok" ? " is-ok" : shareState === "fail" ? " is-fail" : "")
              }
            >
              {shareLabel}
            </button>
          )}
          {hasMessages && (
            <button onClick={reset} title="New conversation" aria-label="Reset">
              ↺
            </button>
          )}
        </span>
      </header>
      {viewingShared && (
        <div className="ask-chat__shared-banner" role="status">
          <span>Viewing a shared conversation.</span>
          <button type="button" onClick={reset} className="ask-chat__shared-banner-cta">
            Start a new one
          </button>
        </div>
      )}

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
            <MessageView key={m.id} msg={m} onRetry={retry} />
          ))}
        </div>
      )}

      {/* Settle announcements for screen readers. Errors are announced
          via role="alert" on the message itself; the token stream is
          intentionally not in a live region. */}
      <div aria-live="polite" className="sr-only">
        {announcement}
      </div>

      <div className="ask-chat__input-wrap">
        <div className="ask-chat__input">
          <textarea
            ref={taRef}
            rows={1}
            aria-label="Ask a question about Aztec"
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
