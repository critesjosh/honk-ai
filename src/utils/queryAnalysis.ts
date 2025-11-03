/**
 * Query Analysis and Expansion for improved retrieval
 * Analyzes query intent, complexity, and generates query variations
 */

export interface QueryAnalysis {
  type: 'code_search' | 'concept' | 'troubleshooting' | 'how_to' | 'general';
  complexity: 'simple' | 'medium' | 'complex';
  requiresRecent: boolean;
  suggestedDocCount: number;
  keywords: string[];
  expandedQueries: string[];
}

/**
 * Analyze a query to understand its intent and characteristics
 */
export function analyzeQuery(query: string): QueryAnalysis {
  const lowerQuery = query.toLowerCase();

  // Determine query type
  let type: QueryAnalysis['type'] = 'general';
  if (lowerQuery.includes('how') || lowerQuery.includes('implement')) {
    type = 'how_to';
  } else if (lowerQuery.includes('error') || lowerQuery.includes('not working') ||
             lowerQuery.includes('fail') || lowerQuery.includes('issue')) {
    type = 'troubleshooting';
  } else if (lowerQuery.match(/function|class|interface|type|struct|contract/)) {
    type = 'code_search';
  } else if (lowerQuery.includes('what') || lowerQuery.includes('explain') ||
             lowerQuery.includes('difference')) {
    type = 'concept';
  }

  // Determine complexity based on word count and question structure
  const wordCount = query.split(/\s+/).length;
  const complexity: QueryAnalysis['complexity'] =
    wordCount < 5 ? 'simple' :
    wordCount < 15 ? 'medium' : 'complex';

  // Check if requires recent information
  const requiresRecent =
    lowerQuery.includes('latest') ||
    lowerQuery.includes('new') ||
    lowerQuery.includes('recent') ||
    lowerQuery.includes('current') ||
    lowerQuery.includes('updated');

  // Suggested doc count based on complexity and type
  let suggestedDocCount =
    complexity === 'simple' ? 3 :
    complexity === 'medium' ? 5 : 8;

  // Code searches benefit from more examples
  if (type === 'code_search') {
    suggestedDocCount += 2;
  }

  // Extract keywords
  const keywords = extractKeywords(query);

  // Generate query expansions
  const expandedQueries = expandQuery(query, type);

  return {
    type,
    complexity,
    requiresRecent,
    suggestedDocCount,
    keywords,
    expandedQueries,
  };
}

/**
 * Aztec and Noir specific synonym mappings
 */
const AZTEC_SYNONYM_MAP: Record<string, string[]> = {
  // Contracts
  'contract': ['smart contract', 'aztec contract', 'noir contract'],
  'deploy': ['deployment', 'deploying', 'publish'],

  // Privacy
  'private': ['confidential', 'encrypted', 'secret'],
  'public': ['transparent', 'visible', 'open'],

  // Accounts & Identity
  'wallet': ['account', 'signer'],
  'account': ['wallet', 'user'],

  // State & Storage
  'state': ['storage', 'data'],
  'variable': ['var', 'field'],

  // Notes
  'note': ['utxo', 'commitment'],
  'nullifier': ['spent note', 'consumed'],

  // Functions
  'function': ['method', 'fn'],
  'call': ['invoke', 'execute'],

  // Types
  'field': ['felt', 'finite field element'],
  'u8': ['uint8', 'byte'],

  // Noir Language
  'noir': ['aztec.nr', 'noir language'],
  'aztec.nr': ['noir', 'aztec noir'],

  // Protocol Components
  'pxe': ['private execution environment', 'wallet backend'],
  'sequencer': ['block producer', 'validator'],
  'prover': ['proof generator', 'zkp generator'],

  // Development
  'compile': ['build', 'transpile'],
  'test': ['testing', 'unit test'],
  'debug': ['debugging', 'troubleshoot'],

  // Circuits
  'circuit': ['constraint system', 'noir program'],
  'prove': ['generate proof', 'create proof'],
  'verify': ['check proof', 'validate proof'],
};

/**
 * Expand query with synonyms and variations
 */
function expandQuery(query: string, type: QueryAnalysis['type']): string[] {
  const expansions = [query]; // Original query always included

  // Find and replace terms with synonyms
  Object.entries(AZTEC_SYNONYM_MAP).forEach(([term, synonyms]) => {
    const regex = new RegExp(`\\b${term}\\b`, 'gi');
    if (regex.test(query)) {
      // Add one variation per synonym (limit to avoid explosion)
      synonyms.slice(0, 2).forEach(synonym => {
        const expanded = query.replace(regex, synonym);
        if (!expansions.includes(expanded)) {
          expansions.push(expanded);
        }
      });
    }
  });

  // Add query reformulations based on type
  if (type === 'how_to') {
    // Add "example" variant for how-to queries
    if (!query.toLowerCase().includes('example')) {
      expansions.push(`${query} example`);
    }
  } else if (type === 'troubleshooting') {
    // Add "fix" variant for troubleshooting
    if (!query.toLowerCase().includes('fix') && !query.toLowerCase().includes('solve')) {
      expansions.push(`how to fix ${query}`);
    }
  }

  // Limit to 3 variations to avoid too many queries
  return expansions.slice(0, 3);
}

/**
 * Extract meaningful keywords from query
 */
function extractKeywords(query: string): string[] {
  // Common stop words to filter out
  const stopWords = new Set([
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'as', 'is', 'are', 'was', 'were', 'be',
    'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
    'would', 'should', 'could', 'can', 'may', 'might', 'must', 'shall',
    'how', 'what', 'when', 'where', 'why', 'who', 'which',
  ]);

  // Extract words, filter stop words and short words
  const words = query
    .toLowerCase()
    .replace(/[^\w\s]/g, ' ') // Remove punctuation
    .split(/\s+/)
    .filter(word =>
      word.length > 2 &&
      !stopWords.has(word) &&
      !/^\d+$/.test(word) // Filter pure numbers
    );

  // Deduplicate
  return [...new Set(words)];
}

/**
 * Calculate adaptive document count based on query analysis
 */
export function getAdaptiveDocCount(
  analysis: QueryAnalysis,
  maxDocs: number = 8
): number {
  // Base count from query complexity
  let count = analysis.suggestedDocCount;

  // Adjust based on query type
  if (analysis.type === 'code_search') {
    count += 1; // Code searches benefit from more examples
  } else if (analysis.complexity === 'simple') {
    count -= 1; // Simple queries need fewer docs
  }

  // Ensure within reasonable bounds
  return Math.min(Math.max(count, 2), maxDocs);
}
