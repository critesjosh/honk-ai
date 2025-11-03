/**
 * Relevance Filtering and Adaptive Retrieval
 * Filters low-quality results and adjusts retrieval count based on query characteristics
 */

import { SimilaritySearchResponse } from '../types/data.js';
import { QueryAnalysis } from './queryAnalysis.js';
import { HybridSearchResult } from './hybridSearch.js';

export interface FilterConfig {
  minSimilarity: number;        // Default: 0.3 (distance < 0.7)
  maxResults: number;           // Default: 8
  minResults: number;           // Default: 2
}

/**
 * Filter search results by relevance threshold
 * Removes results with similarity below threshold while respecting min/max bounds
 */
export function filterByRelevance(
  results: SimilaritySearchResponse | HybridSearchResult,
  config: FilterConfig
): SimilaritySearchResponse {
  const maxDistance = 1 - config.minSimilarity;

  const filtered: SimilaritySearchResponse = {
    ids: [],
    embeddings: [[]] as number[][],
    documents: [],
    metadatas: [[]],
    distances: [[]],
  };

  // Determine which distance/score array to use
  const distances = results.distances?.[0] || [];
  const hasScores = 'scores' in results && results.scores;

  results.documents.forEach((doc, idx) => {
    // Get distance (lower is better) or convert score to distance
    let distance: number;
    if (hasScores && results.scores) {
      // Convert score (higher is better) to distance (lower is better)
      distance = 1 - results.scores[idx];
    } else {
      distance = distances[idx] || 0;
    }

    // Filter by distance threshold
    if (distance <= maxDistance) {
      filtered.ids.push(results.ids[idx]);
      filtered.documents.push(doc);
      filtered.metadatas[0].push(results.metadatas?.[0]?.[idx] || null);

      // Only try to access embeddings if it's a SimilaritySearchResponse
      if ('embeddings' in results && results.embeddings?.[0]?.[idx]) {
        filtered.embeddings[0].push(results.embeddings[0][idx]);
      }
      if (filtered.distances) {
        filtered.distances[0].push(distance);
      }
    }
  });

  // Ensure we have at least minResults (even if below threshold)
  if (filtered.documents.length < config.minResults) {
    console.warn(
      `Only ${filtered.documents.length} docs passed filter (threshold: ${config.minSimilarity}). ` +
      `Threshold may be too strict.`
    );

    // If we have too few results, relax the filter and take the best available
    if (filtered.documents.length === 0 && results.documents.length > 0) {
      // Take at least the top result even if it doesn't pass threshold
      const topIdx = 0;
      filtered.ids.push(results.ids[topIdx]);
      filtered.documents.push(results.documents[topIdx]);
      filtered.metadatas[0].push(results.metadatas?.[0]?.[topIdx] || null);
      if ('embeddings' in results && results.embeddings?.[0]?.[topIdx]) {
        filtered.embeddings[0].push(results.embeddings[0][topIdx]);
      }
      if (filtered.distances) {
        filtered.distances[0].push(distances[topIdx] || 0);
      }
    }
  }

  // Trim to maxResults
  if (filtered.documents.length > config.maxResults) {
    filtered.documents = filtered.documents.slice(0, config.maxResults);
    filtered.ids = filtered.ids.slice(0, config.maxResults);
    filtered.metadatas[0] = filtered.metadatas[0].slice(0, config.maxResults);
    if (filtered.embeddings[0].length > 0) {
      filtered.embeddings[0] = filtered.embeddings[0].slice(0, config.maxResults);
    }
    if (filtered.distances && filtered.distances[0].length > 0) {
      filtered.distances[0] = filtered.distances[0].slice(0, config.maxResults);
    }
  }

  return filtered;
}

/**
 * Calculate adaptive document count based on query analysis
 */
export function getAdaptiveDocCount(
  analysis: QueryAnalysis,
  envMaxDocs: number = 8
): number {
  // Base count from query complexity
  let count = analysis.suggestedDocCount;

  // Adjust based on query type
  if (analysis.type === 'code_search') {
    count += 2; // Code searches benefit from more examples
  } else if (analysis.type === 'troubleshooting') {
    count += 1; // Troubleshooting may need multiple solutions
  } else if (analysis.complexity === 'simple') {
    count -= 1; // Simple queries need fewer docs
  }

  // Respect environment limits
  return Math.min(Math.max(count, 2), envMaxDocs);
}

/**
 * Deduplicate documents that are very similar (near-duplicates)
 * Useful when hybrid search returns similar chunks from same document
 */
export function deduplicateResults(
  results: SimilaritySearchResponse,
  similarityThreshold: number = 0.9
): SimilaritySearchResponse {
  const deduplicated: SimilaritySearchResponse = {
    ids: [],
    embeddings: [[]] as number[][],
    documents: [],
    metadatas: [[]],
    distances: [[]],
  };

  const seen = new Set<string>();

  results.documents.forEach((doc, idx) => {
    // Create a simplified version for comparison
    const normalized = doc.toLowerCase().replace(/\s+/g, ' ').trim();

    // Check if we've seen a very similar document
    let isDuplicate = false;
    for (const seenDoc of seen) {
      const similarity = calculateStringSimilarity(normalized, seenDoc);
      if (similarity > similarityThreshold) {
        isDuplicate = true;
        break;
      }
    }

    if (!isDuplicate) {
      seen.add(normalized);
      deduplicated.ids.push(results.ids[idx]);
      deduplicated.documents.push(doc);
      deduplicated.metadatas[0].push(results.metadatas?.[0]?.[idx] || null);

      if (results.embeddings && results.embeddings[0] && results.embeddings[0][idx]) {
        deduplicated.embeddings[0].push(results.embeddings[0][idx]);
      }
      if (results.distances && results.distances[0] && results.distances[0][idx] !== undefined) {
        if (deduplicated.distances) {
          deduplicated.distances[0].push(results.distances[0][idx]);
        }
      }
    }
  });

  return deduplicated;
}

/**
 * Calculate string similarity using Jaccard similarity on word sets
 */
function calculateStringSimilarity(str1: string, str2: string): number {
  const words1 = new Set(str1.split(/\s+/));
  const words2 = new Set(str2.split(/\s+/));

  // Jaccard similarity: intersection / union
  const intersection = new Set([...words1].filter(x => words2.has(x)));
  const union = new Set([...words1, ...words2]);

  return intersection.size / union.size;
}

/**
 * Log filtering statistics for debugging
 */
export function logFilteringStats(
  original: { count: number; type: string },
  filtered: { count: number },
  config: FilterConfig
): void {
  console.log(
    `Relevance filtering: ${original.count} ${original.type} → ${filtered.count} results ` +
    `(min similarity: ${config.minSimilarity}, max: ${config.maxResults})`
  );
}
