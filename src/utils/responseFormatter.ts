export function chunkLLMResponse(
  response: string,
  maxLength: number,
): string[] {
  const chunks: string[] = [];
  let currentChunk = "";
  let codeBlockIdentifier: string | null = null;

  // Split response into lines for processing
  const lines = response.split("\n");

  for (const line of lines) {
    if (line.startsWith("```")) {
      if (codeBlockIdentifier === null) {
        // Start of a new code block
        codeBlockIdentifier = line;
      } else {
        // End of the current code block
        codeBlockIdentifier = null;
      }
    }

    // Check if adding the line exceeds maxLength
    if (currentChunk.length + line.length + 1 > maxLength) {
      // If we're in a code block, close it before ending the chunk
      if (codeBlockIdentifier) {
        currentChunk += "```\n";
      }

      chunks.push(currentChunk);
      currentChunk = "";

      // If we were in a code block, re-open it in the new chunk
      if (codeBlockIdentifier) {
        currentChunk += codeBlockIdentifier + "\n";
      }
    }

    currentChunk += line + "\n";
  }

  // Add any remaining text as a chunk
  if (currentChunk) {
    chunks.push(currentChunk);
  }

  return chunks;
}

function truncateMiddle(text: string, max: number): string {
  if (text.length <= max) return text;
  const half = Math.floor((max - 3) / 2);
  return text.slice(0, half) + "..." + text.slice(text.length - half);
}

function toTitleCase(text: string): string {
  return text
    .split(/\s+/)
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1).toLowerCase() : w))
    .join(" ");
}

function humanizeSegment(seg: string): string {
  // Replace separators with spaces and title-case
  return toTitleCase(seg.replace(/[-_]+/g, " "));
}

function prettyLabelForUrl(raw: string): { label: string; useMasked: boolean } {
  try {
    const url = new URL(raw);
    const host = url.hostname.replace(/^www\./, "");
    const path = url.pathname.replace(/\/+$/, "");

    // Discord links: show raw, no masked formatting
    if (host.includes("discord.com")) {
      return { label: raw, useMasked: false };
    }

    // GitHub links
    if (host.includes("github.com")) {
      // Extract repo and file path
      const parts = path.split("/").filter(Boolean);
      // Expected: /org/repo/(blob|tree)/branch/...path
      if (parts.length >= 4) {
        const org = parts[0];
        const repo = parts[1];
        const fileParts = parts.slice(4);
        // Line anchors
        const m = url.hash.match(/L(\d+)(?:-L(\d+))?/);
        const lineText = m ? ` L${m[1]}${m[2] ? `–${m[2]}` : ""}` : "";

        // Use last 3 segments of the path for a concise label
        const tailParts = fileParts.slice(-3);
        const hasEllipsis = fileParts.length > tailParts.length;
        const tail = `${hasEllipsis ? "…/" : ""}${tailParts.join("/")}`;
        const base = `${repo}: ${tail}${lineText}`;
        return {
          label: truncateMiddle(base, 90),
          useMasked: true,
        };
      }
      return { label: truncateMiddle(`${host}${path}`, 90), useMasked: true };
    }

    // Docs site (human-friendly breadcrumbs)
    if (host.includes("docs.aztec.network")) {
      const parts = path.split("/").filter(Boolean);
      // Use last 2 meaningful segments for breadcrumb
      const meaningful = parts.slice(-2);
      const crumb = meaningful.map(humanizeSegment).join(" > ");
      const label =
        `Docs: ${crumb || humanizeSegment(parts[parts.length - 1] || "")}`.trim();
      return { label: truncateMiddle(label, 90), useMasked: true };
    }

    // Fallback: host + tail of path
    const fallback = `${host}${path}`;
    return { label: truncateMiddle(fallback, 90), useMasked: true };
  } catch {
    return { label: raw, useMasked: false };
  }
}

export function formatSourcesForDiscord(sources: string[]): string {
  if (sources.length === 0) return "";

  // Deduplicate while preserving order
  const seen = new Set<string>();
  const unique = sources.filter((s) => {
    if (seen.has(s)) return false;
    seen.add(s);
    return true;
  });

  const lines = unique.slice(0, 15).map((s) => {
    const pretty = prettyLabelForUrl(s);
    if (pretty.useMasked) {
      return `- [${pretty.label}](${s})`;
    }
    return `- ${pretty.label}`;
  });

  return `\n---------------------\n**Sources**\n${lines.join("\n")}`;
}

export default {
  chunkLLMResponse,
  formatSourcesForDiscord,
};
