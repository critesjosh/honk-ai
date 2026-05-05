import { useState } from "react";
import type { StreamSource } from "../lib/stream";

// Cap on visible citations. The backend already dedupes by rewritten
// public URL and emits up to 10 sources (`_MAX_SOURCES_EMITTED` in
// api/answer/routes/base.py); we clip to the top 5 here because the
// citation block is meant to be a quick verification surface, not an
// exhaustive bibliography. Order is preserved (descending relevance
// per pgvector distance from the global rerank).
const MAX_SOURCES_DISPLAYED = 5;

// Renders a numbered chip per source, click → poster popover (matches
// the design's `.cite-pop` styling). NOTE: this is intentionally a
// post-message Sources block, not inline `[n]` markers in prose. The
// streamed answer text from /stream is not span-aligned to source
// chunks, so any inline citation we synthesised client-side would be
// fabricated attribution. List-only is what we can defend.

function hostnameOf(url: string | undefined): string {
  if (!url) return "source";
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

function shortLabel(s: StreamSource): string {
  const url = s.source;
  if (!url) return s.title || "source";
  try {
    const u = new URL(url);
    return u.pathname.replace(/^\//, "").replace(/\/$/, "") || u.hostname;
  } catch {
    return url;
  }
}

export function SourcesList({ sources }: { sources: StreamSource[] }) {
  const [active, setActive] = useState<number | null>(null);
  if (!sources.length) return null;
  const visible = sources.slice(0, MAX_SOURCES_DISPLAYED);

  return (
    <div className="msg__sources" onClick={(e) => e.stopPropagation()}>
      <span className="msg__sources-label">Sources</span>
      {visible.map((s, idx) => {
        const num = idx + 1;
        const isActive = active === idx;
        return (
          <span key={`${s.source ?? "src"}-${idx}`} style={{ position: "relative", display: "inline-flex" }}>
            <button
              type="button"
              className={"msg__source"}
              onClick={(e) => {
                e.stopPropagation();
                setActive(isActive ? null : idx);
              }}
              aria-expanded={isActive}
              aria-label={`Source ${num}: ${hostnameOf(s.source)}`}
            >
              <span className="n">{num}</span>
              {shortLabel(s)}
            </button>
            {isActive && (
              <span
                className="cite-pop"
                style={{ top: "calc(100% + 8px)", left: 0 }}
                onClick={(e) => e.stopPropagation()}
              >
                <span className="cite-pop__head">
                  <span className="cite-pop__num">{num}</span>
                  <span className="cite-pop__src">{hostnameOf(s.source)}</span>
                </span>
                {s.title && <h4 className="cite-pop__title">{s.title}</h4>}
                {s.text && <p className="cite-pop__excerpt">{s.text}</p>}
                {s.source && (
                  <a
                    className="cite-pop__link"
                    href={s.source}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    Open source <span>↗</span>
                  </a>
                )}
              </span>
            )}
          </span>
        );
      })}
    </div>
  );
}
