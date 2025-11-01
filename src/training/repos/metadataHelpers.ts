import { DocumentMetadata } from "./parsing/common.js";

/**
 * Determine file type from file path
 */
export function getFileType(path: string): DocumentMetadata['fileType'] {
  const extension = path.split('.').pop()?.toLowerCase();

  switch (extension) {
    case 'md':
    case 'mdx':
    case 'markdown':
      return 'markdown';
    case 'ts':
    case 'tsx':
      return 'typescript';
    case 'js':
    case 'jsx':
      return 'javascript';
    case 'nr':
    case 'noir':
      return 'noir';
    case 'rs':
      return 'rust';
    default:
      return undefined;
  }
}

/**
 * Get language identifier from file path
 */
export function getLanguage(path: string): string | undefined {
  const fileType = getFileType(path);
  if (!fileType) return undefined;

  const languageMap: Record<string, string> = {
    'markdown': 'markdown',
    'typescript': 'typescript',
    'javascript': 'javascript',
    'noir': 'noir',
    'rust': 'rust',
  };

  return languageMap[fileType];
}

/**
 * Calculate importance score based on file path characteristics
 * Score ranges from 0 to 1, with higher values indicating more important documents
 */
export function calculateImportance(path: string): number {
  let score = 0.5; // Base score

  // Official docs get higher score
  if (path.startsWith('docs/')) {
    score += 0.3;
  }

  // Examples get moderate boost
  if (path.includes('/examples/') || path.includes('/example/')) {
    score += 0.2;
  }

  // Reduce score based on directory depth
  const depth = path.split('/').length - 1;
  score -= Math.min(depth * 0.05, 0.3);

  // Important files get boost
  const filename = path.split('/').pop()?.toLowerCase() || '';
  if (['readme.md', 'index.md', 'index.ts', 'getting-started.md'].includes(filename)) {
    score += 0.2;
  }

  // Test files get lower score
  if (path.includes('/test/') || path.includes('/__tests__/') || filename.includes('.test.')) {
    score -= 0.3;
  }

  // Clamp between 0 and 1
  return Math.max(0, Math.min(1, score));
}

/**
 * Extract heading path from markdown content for a specific chunk title
 * Returns the hierarchical path of headings leading to this section
 */
export function extractHeadingPath(content: string, chunkTitle: string): string[] {
  const lines = content.split('\n');
  const headings: Array<{ level: number; text: string }> = [];

  // Extract all headings
  for (const line of lines) {
    const match = line.match(/^(#{1,6})\s+(.+)$/);
    if (match) {
      const level = match[1].length;
      const text = match[2].trim();
      headings.push({ level, text });
    }
  }

  // Find the target heading
  const targetIndex = headings.findIndex(h => h.text === chunkTitle);
  if (targetIndex === -1) return [chunkTitle];

  // Build path by including parent headings
  const path: string[] = [];
  const targetLevel = headings[targetIndex].level;

  // Add all parent headings
  for (let i = targetIndex - 1; i >= 0; i--) {
    if (headings[i].level < targetLevel) {
      path.unshift(headings[i].text);
      if (headings[i].level === 1) break; // Stop at top-level heading
    }
  }

  // Add target heading
  path.push(chunkTitle);

  return path;
}

/**
 * Get heading level from a markdown heading string
 */
export function getHeadingLevel(title: string): number | undefined {
  // Try to extract level from markdown syntax in title
  const match = title.match(/^#{1,6}/);
  if (match) {
    return match[0].length;
  }

  // Default to undefined if not a heading
  return undefined;
}

/**
 * Check if a file path indicates this is an example
 */
export function isExampleFile(path: string): boolean {
  return path.includes('/examples/') ||
         path.includes('/example/') ||
         path.includes('example') ||
         path.startsWith('examples/');
}

/**
 * Check if a file path indicates this is official documentation
 */
export function isDocumentationFile(path: string): boolean {
  return path.startsWith('docs/') ||
         path.includes('/docs/') ||
         path.endsWith('README.md');
}
