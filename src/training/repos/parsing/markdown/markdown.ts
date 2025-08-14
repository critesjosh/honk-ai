import { Data } from "../common.js";

export interface MdxSection {
  /** The heading text of this section (or "No heading" if none) */
  title: string;
  /** Clean text lines in this section (Markdown-like) */
  content: string;
  /** 0 if no heading, or 1..6 for #..###### */
  level: number;
  /** For debugging/tracking */
  start: number;
  end: number;
}

/**
 * Parses an MDX file into sections.
 * Extracts front matter, splits content into lines, cleans HTML/MDX syntax,
 * and groups lines into sections based on markdown headings.
 */
export function parseMdxFile(rawContent: string): MdxSection[] {
  // Extract front matter
  const frontmatterMatch = rawContent.match(
    /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/m,
  );
  let remainder = rawContent;
  if (frontmatterMatch) {
    remainder = rawContent.slice(
      frontmatterMatch.index! + frontmatterMatch[0].length,
    );
  }

  // Split into lines and clean them
  const lines = remainder.split(/\r?\n/);
  const cleanedLines: string[] = [];

  let inCodeBlock = false;

  for (const line of lines) {
    let txt = line.trim();
    if (!txt) {
      cleanedLines.push("");
      continue;
    }

    // Check for code block markers
    if (txt.match(/^```/)) {
      inCodeBlock = !inCodeBlock;
      cleanedLines.push(txt);
      continue;
    }

    // Preserve code blocks as-is
    if (inCodeBlock) {
      cleanedLines.push(line);
      continue;
    }

    // Clean MDX/HTML syntax outside code blocks
    txt = txt.replace(/<[^>]*>/g, ""); // Remove HTML tags
    txt = txt.replace(/\b[\w-]+\s*=\s*\{[^}]*\}/g, ""); // Remove MDX props
    txt = txt.replace(/\{[^}]*\}/g, ""); // Remove brace expressions
    txt = txt.replace(/'}\\n'\s*\+/g, "");
    txt = txt.replace(/">\\n'\s*\+/g, "");
    txt = txt.replace(/"}\n' \+/g, "");
    txt = txt.trim();

    // Only keep lines with meaningful content
    if (!/[a-zA-Z0-9.,;:!?()[\]]/.test(txt)) continue;
    cleanedLines.push(txt);
  }

  // Group lines into sections based on headings
  const sections: MdxSection[] = [];
  let currentSection: MdxSection | null = null;
  const seenHeadings = new Set<string>();

  inCodeBlock = false;
  let beforeFirstHeadingContent = "";
  let hasFoundFirstHeading = false;

  for (let i = 0; i < cleanedLines.length; i++) {
    const line = cleanedLines[i];

    // Track code blocks for heading detection
    if (line.match(/^```/)) {
      inCodeBlock = !inCodeBlock;

      if (!hasFoundFirstHeading) {
        beforeFirstHeadingContent += line + "\n";
      } else if (currentSection) {
        currentSection.content += line + "\n";
      }
      continue;
    }

    // Detect headings outside code blocks
    if (!inCodeBlock && line.match(/^#{1,6}\s+/)) {
      hasFoundFirstHeading = true;

      const level = line.match(/^(#+)/)?.[1].length || 0;
      const title = line.replace(/^#+\s+/, "").trim();

      // Skip duplicate headings
      if (seenHeadings.has(title)) {
        continue;
      }
      seenHeadings.add(title);

      currentSection = {
        title,
        content: line + "\n",
        level,
        start: i,
        end: i,
      };

      sections.push(currentSection);
    } else {
      if (!hasFoundFirstHeading) {
        beforeFirstHeadingContent += line + "\n";
      } else if (currentSection) {
        currentSection.content += line + "\n";
        currentSection.end = i;
      }
    }
  }

  // Add content before first heading as level 0 section
  if (
    beforeFirstHeadingContent.trim() &&
    !sections.some((s) => s.level === 0)
  ) {
    sections.unshift({
      title: "",
      content: beforeFirstHeadingContent,
      level: 0,
      start: 0,
      end: hasFoundFirstHeading
        ? sections[0].start - 1
        : cleanedLines.length - 1,
    });
  }

  // Return only heading-based sections (level > 0)
  return sections.filter((s) => s.level > 0);
}

/**
 * Splits the MDX file into chunks as Data objects with proper hierarchical chunking:
 * - Front matter is preserved in every chunk
 * - Each section includes its hierarchical parents for context
 * - Subsections (h3) include their parent sections (h2) etc.
 * - Small chunks are automatically merged to avoid fragmentation
 */
export function chunkMdxFile(rawContent: string): Data[] {
  // Extract front matter
  const frontmatterMatch = rawContent.match(
    /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/m,
  );
  let frontmatterRaw = "";

  if (frontmatterMatch) {
    frontmatterRaw = frontmatterMatch[0];
    rawContent = rawContent.slice(
      frontmatterMatch.index! + frontmatterMatch[0].length,
    );
  }

  // Parse into sections
  const sections = parseMdxFile(rawContent);

  // Handle case with no headings
  if (sections.length === 0) {
    const lines = rawContent.split(/\r?\n/);
    const title = lines[0]?.trim() || "No title";
    return [
      {
        title: `${title} (0-${lines.length - 1})`,
        content: cleanContent(frontmatterRaw + rawContent),
        start: 0,
        end: lines.length - 1,
      },
    ];
  }

  const rawChunks: Data[] = [];

  // Create initial chunks with hierarchical context
  for (let i = 0; i < sections.length; i++) {
    const currentSection = sections[i];
    const parentSections = findParentSections(sections, i);

    // Build hierarchical content
    let content = frontmatterRaw ? frontmatterRaw + "\n" : "";

    // Add all parent sections for context
    for (const parent of parentSections) {
      content += parent.content + "\n";
    }

    // Add current section
    content += currentSection.content;

    // Build title with hierarchy
    const titleParts = parentSections
      .map((p) => p.title)
      .concat(currentSection.title);
    const hierarchicalTitle = titleParts.join(" + ");

    rawChunks.push({
      title: `${hierarchicalTitle} (${currentSection.start}-${currentSection.end})`,
      content: cleanContent(content.trim()),
      start: currentSection.start,
      end: currentSection.end,
    });
  }

  // Merge small chunks intelligently
  return mergeSmallChunks(rawChunks);
}

/**
 * Clean up excessive newlines and whitespace in content
 */
function cleanContent(content: string): string {
  return (
    content
      // Replace multiple consecutive newlines with double newlines (paragraph breaks)
      .replace(/\n{3,}/g, "\n\n")
      // Remove trailing whitespace from each line
      .split("\n")
      .map((line) => line.trimEnd())
      .join("\n")
      // Remove any trailing newlines at the very end
      .replace(/\n+$/, "")
  );
}

/**
 * Find parent sections for a given section index based on heading hierarchy
 */
function findParentSections(
  sections: MdxSection[],
  currentIndex: number,
): MdxSection[] {
  const currentSection = sections[currentIndex];
  const parents: MdxSection[] = [];
  let targetLevel = currentSection.level - 1;

  // Look backwards for parent sections with progressively lower heading levels
  for (let i = currentIndex - 1; i >= 0 && targetLevel > 0; i--) {
    const section = sections[i];

    // If this section matches our target level, it's a direct parent
    if (section.level === targetLevel) {
      parents.unshift(section);
      targetLevel--; // Look for the next level up (grandparent)
    }
  }

  return parents;
}

/**
 * Alternative parsing approach that creates individual chunks for each section
 * without hierarchical combination - useful for very large documents
 */
export function parseMarkdownSections(content: string): Data[] {
  const sections = parseMdxFile(content);
  const chunks: Data[] = [];

  // Extract front matter
  const frontmatterMatch = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/m);
  const frontmatterRaw = frontmatterMatch ? frontmatterMatch[0] : "";

  for (const section of sections) {
    let sectionContent = frontmatterRaw ? frontmatterRaw + "\n" : "";
    sectionContent += section.content;

    chunks.push({
      title: `${section.title} (${section.start}-${section.end})`,
      content: sectionContent.trim(),
      start: section.start,
      end: section.end,
    });
  }

  return chunks;
}

/**
 * Specialized parser for documentation that maintains table of contents context
 */
export function parseDocumentation(content: string): Data[] {
  const sections = parseMdxFile(content);
  if (sections.length === 0) {
    return chunkMdxFile(content);
  }

  const chunks: Data[] = [];
  const frontmatterMatch = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/m);
  const frontmatterRaw = frontmatterMatch ? frontmatterMatch[0] : "";

  // Create table of contents
  const toc = sections
    .map((s) => `${"  ".repeat(s.level - 1)}- ${s.title}`)
    .join("\n");

  const tocSection = `# Table of Contents\n${toc}\n\n`;

  // Create chunks with TOC context
  for (const section of sections) {
    let chunkContent = frontmatterRaw ? frontmatterRaw + "\n" : "";
    chunkContent += tocSection;
    chunkContent += section.content;

    chunks.push({
      title: `${section.title} (${section.start}-${section.end})`,
      content: chunkContent.trim(),
      start: section.start,
      end: section.end,
    });
  }

  return mergeSmallChunks(chunks);
}

/**
 * Merge small chunks intelligently to avoid fragmentation
 */
function mergeSmallChunks(chunks: Data[]): Data[] {
  const MIN_CHUNK_SIZE = 500; // Minimum characters per chunk (increased)
  const MAX_MERGED_CHUNK_SIZE = 2000; // Maximum size for merged chunks
  const merged: Data[] = [];

  let i = 0;
  while (i < chunks.length) {
    let currentChunk = chunks[i];

    // Calculate the actual content size (excluding frontmatter duplication)
    const contentWithoutFrontmatter = stripFrontmatter(currentChunk.content);

    // If chunk is already large enough, keep it as-is
    if (contentWithoutFrontmatter.length >= MIN_CHUNK_SIZE) {
      merged.push(currentChunk);
      i++;
      continue;
    }

    // Try to merge small chunks together
    const mergeableChunks = [currentChunk];
    let totalContentLength = contentWithoutFrontmatter.length;
    let j = i + 1;

    // Look ahead to merge consecutive small chunks
    while (j < chunks.length) {
      const nextChunk = chunks[j];
      const nextContentLength = stripFrontmatter(nextChunk.content).length;

      // Stop if adding this chunk would exceed the limit
      if (totalContentLength + nextContentLength > MAX_MERGED_CHUNK_SIZE) {
        break;
      }

      // Stop if the next chunk is already large enough and we have at least one small chunk
      if (nextContentLength >= MIN_CHUNK_SIZE && mergeableChunks.length > 0) {
        break;
      }

      mergeableChunks.push(nextChunk);
      totalContentLength += nextContentLength;
      j++;

      // Stop if we've reached a good merged size
      if (totalContentLength >= MIN_CHUNK_SIZE) {
        break;
      }
    }

    // Create merged chunk if we have multiple small chunks OR if merging makes it reach min size
    if (
      mergeableChunks.length > 1 ||
      (mergeableChunks.length === 1 &&
        totalContentLength < MIN_CHUNK_SIZE &&
        j < chunks.length)
    ) {
      const titles = mergeableChunks.map((c) => extractMainTitle(c.title));
      const uniqueTitles = [...new Set(titles)].filter((t) => t.trim()); // Remove duplicates and empty
      const mergedTitle =
        uniqueTitles.length > 0 ? uniqueTitles.join(" + ") : "Merged Sections";

      const mergedContent = mergeableChunks
        .map((c) => stripFrontmatter(c.content))
        .filter((content) => content.trim())
        .join("\n\n---\n\n"); // Separator between merged sections

      // Add frontmatter back to the beginning
      const frontmatter = extractFrontmatter(currentChunk.content);
      const finalContent = frontmatter + mergedContent;

      merged.push({
        title: `${mergedTitle} (${currentChunk.start}-${mergeableChunks[mergeableChunks.length - 1].end})`,
        content: cleanContent(finalContent),
        start: currentChunk.start,
        end: mergeableChunks[mergeableChunks.length - 1].end,
      });

      console.log(
        `Merged ${mergeableChunks.length} small chunks into: ${mergedTitle} (${totalContentLength} chars)`,
      );
    } else {
      // Single chunk, keep as-is even if small
      merged.push(currentChunk);
    }

    i = j;
  }

  return merged;
}

/**
 * Extract main title from a potentially hierarchical title
 */
function extractMainTitle(title: string): string {
  // Remove line range info: "Title (1-10)" -> "Title"
  const withoutRange = title.replace(/\s*\(\d+-\d+\)$/, "");

  // Take the last part of hierarchical titles: "A + B + C" -> "C"
  const parts = withoutRange.split(" + ");
  return parts[parts.length - 1].trim();
}

/**
 * Extract frontmatter from content if present
 */
function extractFrontmatter(content: string): string {
  const frontmatterMatch = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/m);
  return frontmatterMatch ? frontmatterMatch[0] + "\n" : "";
}

/**
 * Remove frontmatter from content
 */
function stripFrontmatter(content: string): string {
  const frontmatterMatch = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/m);
  if (frontmatterMatch) {
    return content
      .slice(frontmatterMatch.index! + frontmatterMatch[0].length)
      .trim();
  }
  return content;
}
