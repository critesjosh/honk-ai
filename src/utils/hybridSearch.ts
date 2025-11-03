/**
 * Hybrid Search combining Vector Search (semantic) with BM25 (keyword-based)
 * Provides better retrieval by balancing both approaches
 */

import { BM25Retriever } from '@langchain/community/retrievers/bm25';
import { Document } from '@langchain/core/documents';
import { returnAllDocuments, getDocumentCount } from './chroma.js';
import { similaritySearch } from './chroma.js';
import { SimilaritySearchResponse } from '../types/data.js';

export interface HybridSearchParams {
  query: string;
  embedding: number[];
  vectorWeight?: number;    // Default: 0.7
  keywordWeight?: number;   // Default: 0.3
  topK?: number;            // Default: 10
}

export interface HybridSearchResult {
  ids: string[];
  documents: string[];
  metadatas: any[];
  scores: number[];  // Combined scores
  distances?: number[][]; // For compatibility with ChromaDB response
}

// Cache BM25 retriever to avoid rebuilding on every query
let bm25RetrieverCache: BM25Retriever | null = null;
let lastBuildTime = 0;
let documentCount = 0;

/**
 * Get or build BM25 retriever from ChromaDB documents
 * Rebuilds cache every hour or if document count changes
 */
async function getBM25Retriever(): Promise<BM25Retriever> {
  const now = Date.now();
  const oneHour = 60 * 60 * 1000;

  // Check current document count efficiently (without fetching all docs)
  const currentDocCount = await getDocumentCount();

  // Rebuild cache if:
  // 1. Cache doesn't exist
  // 2. More than an hour has passed
  // 3. Document count has changed
  if (!bm25RetrieverCache ||
      (now - lastBuildTime) > oneHour ||
      currentDocCount !== documentCount) {
    console.log('Building BM25 retriever from ChromaDB...');

    // Only NOW fetch all documents for building the index
    const allDocs = await returnAllDocuments();

    // Convert to LangChain document format
    const documents = allDocs.map(doc => new Document({
      pageContent: doc.text,
      metadata: { source: doc.source, title: doc.title }
    }));

    bm25RetrieverCache = BM25Retriever.fromDocuments(documents, {
      k: 20, // Retrieve more for later filtering
    });

    lastBuildTime = now;
    documentCount = currentDocCount;
    console.log(`BM25 retriever built with ${documents.length} documents`);
  }

  return bm25RetrieverCache;
}

/**
 * Perform hybrid search combining vector and keyword approaches
 */
export async function hybridSearch(
  params: HybridSearchParams
): Promise<HybridSearchResult> {
  const {
    query,
    embedding,
    vectorWeight = 0.7,
    keywordWeight = 0.3,
    topK = 10
  } = params;

  // Perform vector search
  const vectorResults = await similaritySearch({
    inputEmbedding: embedding,
    params: { limit: topK * 2 } // Get more for merging
  });

  // Perform keyword search
  const bm25 = await getBM25Retriever();
  const keywordDocs = await bm25.invoke(query);

  // Merge results with weighted scores
  const merged = mergeWithWeights(
    vectorResults,
    keywordDocs,
    vectorWeight,
    keywordWeight
  );

  // Sort by combined score (descending) and take top K
  const sorted = merged
    .sort((a, b) => b.score - a.score)
    .slice(0, topK);

  // Convert distances back to array format for compatibility
  const distances = sorted.map(r => 1 - r.score); // Convert score back to distance

  return {
    ids: sorted.map(r => r.id),
    documents: sorted.map(r => r.document),
    metadatas: sorted.map(r => r.metadata),
    scores: sorted.map(r => r.score),
    distances: [distances],
  };
}

/**
 * Merge vector and keyword results with weighted scoring
 */
function mergeWithWeights(
  vectorResults: SimilaritySearchResponse,
  keywordDocs: Document[],
  vectorWeight: number,
  keywordWeight: number
): Array<{ id: string; document: string; metadata: any; score: number }> {
  const resultMap = new Map<string, any>();

  // Add vector results
  vectorResults.documents.forEach((doc, idx) => {
    const id = vectorResults.ids[idx];
    const distance = vectorResults.distances?.[0]?.[idx] || 0;
    const vectorScore = 1 - distance; // Convert distance to similarity score

    resultMap.set(id, {
      id,
      document: doc,
      metadata: vectorResults.metadatas?.[0]?.[idx] || {},
      vectorScore,
      keywordScore: 0,
      score: vectorScore * vectorWeight,
    });
  });

  // Add/update with keyword results using reciprocal rank scoring
  keywordDocs.forEach((doc, idx) => {
    // Use title as ID if available, otherwise use content hash
    const id = doc.metadata.title || doc.pageContent.substring(0, 100);
    const keywordScore = 1 / (idx + 1); // Reciprocal rank: 1st=1.0, 2nd=0.5, 3rd=0.33, etc.

    if (resultMap.has(id)) {
      // Document found in both searches - update score
      const existing = resultMap.get(id);
      existing.keywordScore = keywordScore;
      existing.score = (existing.vectorScore * vectorWeight) + (keywordScore * keywordWeight);
    } else {
      // Document only found in keyword search
      resultMap.set(id, {
        id,
        document: doc.pageContent,
        metadata: doc.metadata,
        vectorScore: 0,
        keywordScore,
        score: keywordScore * keywordWeight,
      });
    }
  });

  return Array.from(resultMap.values());
}

/**
 * Clear the BM25 cache (useful when documents are updated)
 */
export function clearBM25Cache(): void {
  bm25RetrieverCache = null;
  lastBuildTime = 0;
  documentCount = 0;
  console.log('BM25 cache cleared');
}
