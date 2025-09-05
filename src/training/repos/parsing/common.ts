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