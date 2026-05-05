import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { SourcesList } from "./SourcesList";
import type { StreamSource } from "../lib/stream";

export type ChatMessage =
  | { id: number; role: "user"; text: string }
  | {
      id: number;
      role: "bot";
      text: string;
      sources: StreamSource[];
      streaming: boolean;
      error?: string;
    };

export function MessageView({ msg }: { msg: ChatMessage }) {
  if (msg.role === "user") {
    return (
      <div className="msg msg--user">
        <div className="msg__role">
          <span className="dot"></span>You
        </div>
        <div className="msg__body">{msg.text}</div>
      </div>
    );
  }

  return (
    <div className="msg msg--bot">
      <div className="msg__role">
        <span className="dot"></span>Ask Aztec
      </div>
      <div className="msg__body">
        {msg.streaming && !msg.text && !msg.error ? (
          <span className="typing">
            <span></span>
            <span></span>
            <span></span>
          </span>
        ) : msg.error ? (
          <p style={{ color: "var(--vermillion-shade-1)" }}>{msg.error}</p>
        ) : (
          <>
            <BotProse text={msg.text} />
            {msg.streaming && <span className="caret"></span>}
          </>
        )}
        {!msg.streaming && msg.sources.length > 0 && (
          <SourcesList sources={msg.sources} />
        )}
      </div>
    </div>
  );
}

// Render bot prose as Markdown (GFM: tables, strikethrough, task lists,
// fenced code, autolinks). react-markdown does NOT render raw HTML by
// default, so the LLM cannot inject script/iframe/onerror handlers via
// the streamed text — important since the bundle has no LLM-side
// allowlist between the model and our DOM.
//
// We intentionally do NOT post-process for inline `[n]` markers — the
// answer stream isn't span-aligned to source chunks, so injecting
// markers would fabricate attribution. Citations live in the
// SourcesList block under the body.
function BotProse({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Force every link to open in a new tab with safe rel.
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
