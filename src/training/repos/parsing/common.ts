/**
 * Enhanced metadata for documents stored in ChromaDB
 */
export interface DocumentMetadata {
  // Existing
  link: string;
  date: number;

  // File Information
  fileType?: 'markdown' | 'typescript' | 'javascript' | 'noir' | 'rust';
  language?: string;
  filePath?: string;

  // Content Structure
  headingLevel?: number;           // For markdown sections
  headingPath?: string[];          // e.g., ['Getting Started', 'Installation', 'Prerequisites']

  // Version Information
  versionTag?: string;             // From AZTEC_DOCS_VERSION or git tag
  lastModified?: number;           // Git last modified timestamp

  // Content Classification
  isExample?: boolean;             // Is this a code example?
  isDocumentation?: boolean;       // Official docs vs community content
  importance?: number;             // 0-1 score based on centrality

  // Code-specific
  codeType?: 'function' | 'class' | 'interface' | 'type' | 'contract';
  exports?: string[];              // Exported symbols
  imports?: string[];              // Key imports
}

/**
 * Common interface for parsed code or content blocks across all parsers.
 */
export interface Data {
  /** Title or description of the parsed block */
  title: string;
  /** The actual content of the parsed block */
  content: string;
  /** Starting line number in the original source */
  start: number;
  /** Ending line number in the original source */
  end: number;
  /** URL of the original source file */
  url?: string;
  /** Optional metadata for enhanced retrieval */
  metadata?: Partial<DocumentMetadata>;
}

/**
 * Common interface for parsed sections (used by markdown and other hierarchical parsers)
 */
export interface ParsedSection {
  /** The heading text or section identifier */
  title: string;
  /** The content of this section */
  content: string;
  /** Hierarchy level (0 for root, 1-6 for headings, etc.) */
  level: number;
  /** Starting line number in the original source */
  start: number;
  /** Ending line number in the original source */
  end: number;
}

/**
 * File type enumeration for parser selection
 */
export enum FileType {
  NOIR = "noir",
  TYPESCRIPT = "typescript", 
  JAVASCRIPT = "javascript",
  MARKDOWN = "markdown",
  MDX = "mdx",
  UNKNOWN = "unknown"
}

/**
 * Parser configuration options
 */
export interface ParserOptions {
  /** Whether to include import statements in chunks */
  includeImports?: boolean;
  /** Whether to include doc comments */
  includeComments?: boolean;
  /** Maximum chunk size in characters */
  maxChunkSize?: number;
  /** Whether to preserve code formatting */
  preserveFormatting?: boolean;
}

/**
 * Add overlap between consecutive chunks to improve context continuity during retrieval.
 * Takes the last N% of each chunk and prepends it to the next chunk.
 *
 * @param chunks - Array of parsed data chunks
 * @param overlapPercent - Percentage of overlap (0-1), e.g., 0.1 for 10% overlap
 * @returns Array of chunks with overlapping content
 */
export function addOverlapToChunks(chunks: Data[], overlapPercent: number = 0.1): Data[] {
  if (chunks.length <= 1 || overlapPercent <= 0) {
    return chunks;
  }

  const overlappedChunks: Data[] = [];

  for (let i = 0; i < chunks.length; i++) {
    const currentChunk = chunks[i];

    if (i === 0) {
      // First chunk has no previous overlap
      overlappedChunks.push(currentChunk);
      continue;
    }

    const previousChunk = chunks[i - 1];

    // Calculate how much of the previous chunk to overlap
    const overlapLength = Math.floor(previousChunk.content.length * overlapPercent);

    if (overlapLength > 0) {
      // Take the last N% of previous chunk
      const overlapText = previousChunk.content.slice(-overlapLength);

      // Prepend to current chunk with separator
      const overlappedContent = `${overlapText.trim()}\n\n${currentChunk.content}`;

      overlappedChunks.push({
        ...currentChunk,
        content: overlappedContent,
      });
    } else {
      overlappedChunks.push(currentChunk);
    }
  }

  return overlappedChunks;
}