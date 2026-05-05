// Italicized-fragment serif headline. Lifted from the design bundle.
// Splits `text` at character ranges in `frags` and wraps each range in
// <i> so the design's serif italic alt face renders for those letters.

type Range = [number, number];

export function SigHeadline({ text, frags = [] }: { text: string; frags?: Range[] }) {
  if (!frags.length) return <>{text}</>;
  const sorted = [...frags].sort((a, b) => a[0] - b[0]);
  const out: React.ReactNode[] = [];
  let cursor = 0;
  sorted.forEach(([s, e], i) => {
    if (s > cursor) out.push(<span key={`p${i}`}>{text.slice(cursor, s)}</span>);
    out.push(<i key={`i${i}`}>{text.slice(s, e)}</i>);
    cursor = e;
  });
  if (cursor < text.length) out.push(<span key="last">{text.slice(cursor)}</span>);
  return <>{out}</>;
}
