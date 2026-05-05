import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Highlight, themes } from "prism-react-renderer";
import { SourcesList } from "./SourcesList";
import type { StreamSource } from "../lib/stream";

// Languages the LLM emits as `noir` / `nr` should render with Rust
// highlighting since Noir's syntax is Rust-derived and Prism doesn't
// ship a Noir grammar.
const NOIR_LANG_ALIASES = new Set(["noir", "nr"]);

function CodeBlock({ language, value }: { language: string; value: string }) {
  const prismLang = NOIR_LANG_ALIASES.has(language) ? "rust" : language;
  return (
    <Highlight code={value.replace(/\n+$/, "")} language={prismLang} theme={themes.vsDark}>
      {({ className, style, tokens, getLineProps, getTokenProps }) => {
        // Render the highlighted tokens inside a <code> only; let the
        // parent <pre> (from react-markdown's default `pre` component)
        // own the block-level styling (ink background, padding, radius
        // from .md pre in app.css). Strip the theme's backgroundColor
        // so the parent <pre>'s ink fill shows through; per-token
        // colors from getTokenProps still apply.
        const inner = { ...style, backgroundColor: "transparent" };
        return (
          <code className={className} style={inner}>
            {tokens.map((line, i) => (
              <div key={i} {...getLineProps({ line })}>
                {line.map((token, j) => (
                  <span key={j} {...getTokenProps({ token })} />
                ))}
              </div>
            ))}
          </code>
        );
      }}
    </Highlight>
  );
}

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
          // Fenced code blocks → prism-react-renderer (Noir → Rust).
          // Inline code (no `language-*` className) falls through to
          // the default <code> rendering, which is styled via .md code
          // in app.css. react-markdown v9 wraps fenced blocks in
          // <pre><code class="language-foo"> — by handling `code` here
          // and returning the <pre> ourselves, we avoid double <pre>
          // wrappers via the default `pre` component.
          code: ({ className, children, ...props }) => {
            const match = /language-([a-z0-9_+-]+)/i.exec(className ?? "");
            if (!match) {
              // No language tag — could be inline `code` or a fenced
              // block without a language hint. Either way, hand off
              // to the default <code> rendering. For block-with-no-
              // language, react-markdown still wraps in <pre>, which
              // gets `.md pre` styling for an unhighlighted block.
              return (
                <code className={className} {...props}>
                  {children}
                </code>
              );
            }
            return <CodeBlock language={match[1].toLowerCase()} value={String(children)} />;
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
