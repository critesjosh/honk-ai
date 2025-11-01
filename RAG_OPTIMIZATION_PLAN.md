# RAG Optimization Implementation Plan

**Project:** Honk AI - RAG System Improvements
**Timeline:** 2 weeks (medium investment)
**Goal:** Improve retrieval accuracy, reduce outdated information, and enhance response quality

---

## Executive Summary

### Current Pain Points
- ❌ **Outdated information** - returning old/deprecated content instead of latest docs
- ❌ **Wrong context** - retrieving irrelevant documents or misunderstanding queries
- ❌ **Missing information** - unable to find relevant docs that exist in knowledge base

### Expected Outcomes
- **60-80% reduction** in "wrong context" issues via re-ranking + filtering
- **70-90% reduction** in outdated information via version-aware filtering
- **40-60% improvement** in missing information via hybrid search + query expansion
- **Faster responses** via better context window management
- **Higher user satisfaction** via confidence scores and citation validation

---

## Phase 1: Foundation Fixes (Days 1-2) 🔴 CRITICAL

**Priority:** HIGH - Blocking for all other improvements
**Status:** 🔲 Not Started

### Task 1.1: Re-enable Markdown Chunking
**File:** `src/training/repos/index.ts`
**Lines:** 172-183

**Current Issue:**
- Markdown files stored as full documents without chunking
- Chunking code is commented out/paused
- Results in oversized chunks that exceed optimal retrieval size

**Implementation:**
```typescript
// Replace lines 172-183 (currently storing full document)
// With proper chunking:

const chunks = await chunkMdxFile(processedContent);
console.log(`Chunked markdown into ${chunks.length} sections`);

// Add 10% overlap between chunks for context continuity
const chunksWithOverlap = addOverlapToChunks(chunks, 0.1);

// Store each chunk
for (const chunk of chunksWithOverlap) {
  const documentTitle = `${repoUrl}/${relativePath} ${chunk.title}`;
  const documentUrl = convertDocsPathToWebsiteUrl(relativePath);
  const documentContent = `${documentTitle}\n\n${chunk.content}`;

  await UpsertDocument(documentTitle, documentContent, documentUrl);
}
```

**New Function Required:**
```typescript
function addOverlapToChunks(chunks: Data[], overlapPercent: number): Data[] {
  // Implementation: take last N% of previous chunk and prepend to next chunk
}
```

**Testing:**
- [ ] Verify markdown files are chunked (not stored whole)
- [ ] Check chunk sizes are reasonable (500-2000 chars)
- [ ] Confirm overlap is working
- [ ] Re-train on test markdown file and verify chunks in ChromaDB

---

### Task 1.2: Enhanced Metadata System
**Files:**
- `src/utils/chroma.ts` - Update `UpsertDocument` function
- `src/training/repos/index.ts` - Capture metadata during parsing
- `src/training/repos/parsing/*.ts` - Return metadata from parsers
- `src/types/data.ts` - Update type definitions

**Current State:**
```typescript
// Only stores minimal metadata
metadatas: [{ link: source, date: Date.now() }]
```

**New Metadata Schema:**
```typescript
interface DocumentMetadata {
  // Existing
  link: string;
  date: number;

  // NEW - File Information
  fileType: 'markdown' | 'typescript' | 'javascript' | 'noir' | 'rust';
  language: string;
  filePath: string;

  // NEW - Content Structure
  headingLevel?: number;           // For markdown sections
  headingPath?: string[];          // e.g., ['Getting Started', 'Installation', 'Prerequisites']

  // NEW - Version Information
  versionTag: string;              // From AZTEC_DOCS_VERSION or git tag
  lastModified?: number;           // Git last modified timestamp

  // NEW - Content Classification
  isExample: boolean;              // Is this a code example?
  isDocumentation: boolean;        // Official docs vs community content
  importance: number;              // 0-1 score based on centrality

  // NEW - Code-specific
  codeType?: 'function' | 'class' | 'interface' | 'type' | 'contract';
  exports?: string[];              // Exported symbols
  imports?: string[];              // Key imports
}
```

**Implementation Steps:**

1. **Update Type Definitions** (`src/types/data.ts`)
```typescript
export interface DocumentMetadata {
  // Add new interface
}

export interface Data {
  title: string;
  content: string;
  start: number;
  end: number;
  metadata?: Partial<DocumentMetadata>; // NEW
}
```

2. **Update ChromaDB Interface** (`src/utils/chroma.ts`)
```typescript
export async function UpsertDocument(
  id: string,
  document: string,
  source: string,
  metadata: Partial<DocumentMetadata> = {} // NEW parameter
): Promise<boolean> {
  const fullMetadata: DocumentMetadata = {
    link: source,
    date: Date.now(),
    ...metadata
  };

  await collection.upsert({
    documents: [document],
    ids: [id],
    metadatas: [fullMetadata],
  });
}
```

3. **Update Parsers to Return Metadata**

Each parser should return chunks with metadata:
```typescript
// src/training/repos/parsing/typescript/typescript.ts
export async function parseJS(content: string): Promise<Data[]> {
  // ... existing parsing logic

  return chunks.map(chunk => ({
    ...chunk,
    metadata: {
      fileType: 'typescript',
      language: 'typescript',
      codeType: chunk.type, // 'function', 'class', etc.
      exports: chunk.exports,
      imports: chunk.imports,
    }
  }));
}
```

4. **Update Training to Pass Metadata** (`src/training/repos/index.ts`)
```typescript
// In processFile function
const metadata: Partial<DocumentMetadata> = {
  fileType: getFileType(relativePath),
  language: getLanguage(relativePath),
  versionTag: env.AZTEC_DOCS_VERSION || await getGitTag(repoPath),
  isExample: relativePath.includes('/examples/'),
  isDocumentation: relativePath.startsWith('docs/'),
  importance: calculateImportance(relativePath),
  filePath: relativePath,
};

// For markdown
if (isMarkdown) {
  metadata.headingPath = extractHeadingPath(content, chunk.title);
  metadata.headingLevel = getHeadingLevel(chunk.title);
}

await UpsertDocument(documentTitle, documentContent, documentUrl, metadata);
```

5. **Create Helper Functions**
```typescript
function getFileType(path: string): DocumentMetadata['fileType'] {
  // Implementation
}

function calculateImportance(path: string): number {
  // Score based on:
  // - Is it in official docs? (+0.3)
  // - Is it in examples? (+0.2)
  // - Depth in directory structure (-0.1 per level)
  // - Filename patterns (README, index, etc.) (+0.2)
}

function extractHeadingPath(content: string, chunkTitle: string): string[] {
  // Extract full heading hierarchy
}
```

**Testing:**
- [ ] Verify new metadata fields are stored in ChromaDB
- [ ] Check metadata is correct for different file types
- [ ] Confirm version tags are captured correctly
- [ ] Test importance scoring makes sense

---

### Task 1.3: Version-Aware Filtering
**New File:** `src/utils/versionFiltering.ts`

**Purpose:** Prioritize current version docs, deprioritize outdated versions

**Implementation:**
```typescript
import { env } from '../env.js';

interface VersionConfig {
  currentVersion: string;
  boostFactor: number;      // Default: 1.5
  penaltyFactor: number;    // Default: 0.5
}

export function filterAndBoostByVersion(
  results: SimilaritySearchResponse,
  config: VersionConfig
): SimilaritySearchResponse {
  const currentVersion = config.currentVersion || env.AZTEC_DOCS_VERSION;

  if (!currentVersion) {
    return results; // No filtering if version not set
  }

  // Adjust scores based on version match
  results.documents = results.documents.map((doc, idx) => {
    const metadata = results.metadatas[0]?.[idx];
    const docVersion = metadata?.versionTag as string;

    if (!docVersion) return doc;

    let scoreMultiplier = 1.0;

    if (docVersion === currentVersion) {
      // Boost current version
      scoreMultiplier = config.boostFactor;
    } else if (isOutdatedVersion(docVersion, currentVersion)) {
      // Penalize old versions
      scoreMultiplier = config.penaltyFactor;
    }

    // Apply to distance (lower is better)
    if (results.distances?.[0]?.[idx]) {
      results.distances[0][idx] = results.distances[0][idx] / scoreMultiplier;
    }

    return doc;
  });

  // Re-sort by adjusted distances
  return sortByDistance(results);
}

function isOutdatedVersion(docVersion: string, currentVersion: string): boolean {
  // Parse version strings and compare
  // Handle nightly builds, stable releases, etc.
}

function sortByDistance(results: SimilaritySearchResponse): SimilaritySearchResponse {
  // Sort all arrays by distance
}
```

**Integration in `src/utils/llm.ts`:**
```typescript
// After similarity search
let results = await similaritySearch({
  inputEmbedding: embedding,
  params: { limit: env.AMOUNT_OF_DOCS * 2 }, // Get more for filtering
});

// Apply version filtering
results = filterAndBoostByVersion(results, {
  currentVersion: env.AZTEC_DOCS_VERSION || await getLatestVersion(),
  boostFactor: parseFloat(process.env.VERSION_BOOST_FACTOR || '1.5'),
  penaltyFactor: 0.5,
});

// Trim to final count
results = limitResults(results, env.AMOUNT_OF_DOCS);
```

**New Environment Variables:**
```bash
VERSION_BOOST_FACTOR="1.5"    # Multiply score for current version docs
```

**Testing:**
- [ ] Verify current version docs rank higher
- [ ] Confirm old version docs are deprioritized
- [ ] Test with mixed version query results
- [ ] Ensure it works when AZTEC_DOCS_VERSION not set

---

## Phase 2: Retrieval Intelligence (Days 3-5) 🟡 MEDIUM PRIORITY

**Priority:** MEDIUM - Significant impact on "missing information"
**Status:** 🔲 Not Started

### Task 2.1: Query Analysis & Rewriting
**New File:** `src/utils/queryAnalysis.ts`

**Purpose:** Understand query intent and expand ambiguous queries

**Implementation:**
```typescript
export interface QueryAnalysis {
  type: 'code_search' | 'concept' | 'troubleshooting' | 'how_to' | 'general';
  complexity: 'simple' | 'medium' | 'complex';
  requiresRecent: boolean;
  suggestedDocCount: number;
  keywords: string[];
  expandedQueries: string[];
}

export function analyzeQuery(query: string): QueryAnalysis {
  const lowerQuery = query.toLowerCase();

  // Determine query type
  let type: QueryAnalysis['type'] = 'general';
  if (lowerQuery.includes('how') || lowerQuery.includes('implement')) {
    type = 'how_to';
  } else if (lowerQuery.includes('error') || lowerQuery.includes('not working')) {
    type = 'troubleshooting';
  } else if (lowerQuery.match(/function|class|interface|type/)) {
    type = 'code_search';
  } else if (lowerQuery.includes('what') || lowerQuery.includes('explain')) {
    type = 'concept';
  }

  // Determine complexity
  const wordCount = query.split(/\s+/).length;
  const complexity: QueryAnalysis['complexity'] =
    wordCount < 5 ? 'simple' :
    wordCount < 15 ? 'medium' : 'complex';

  // Check if requires recent info
  const requiresRecent =
    lowerQuery.includes('latest') ||
    lowerQuery.includes('new') ||
    lowerQuery.includes('recent') ||
    lowerQuery.includes('current');

  // Suggested doc count based on complexity
  const suggestedDocCount =
    complexity === 'simple' ? 3 :
    complexity === 'medium' ? 5 : 8;

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

function expandQuery(query: string, type: QueryAnalysis['type']): string[] {
  const expansions = [query]; // Original query always included

  // Add Aztec/Noir synonym mappings
  const synonymMap = {
    'contract': ['smart contract', 'aztec contract'],
    'deploy': ['deployment', 'deploying'],
    'wallet': ['account', 'signer'],
    'private': ['confidential', 'encrypted'],
    'public': ['transparent', 'visible'],
    // Add more Aztec-specific synonyms
  };

  // Generate variations
  Object.entries(synonymMap).forEach(([term, synonyms]) => {
    if (query.toLowerCase().includes(term)) {
      synonyms.forEach(syn => {
        expansions.push(query.replace(new RegExp(term, 'gi'), syn));
      });
    }
  });

  return expansions.slice(0, 3); // Limit to 3 variations
}

function extractKeywords(query: string): string[] {
  // Simple keyword extraction
  const stopWords = ['the', 'a', 'an', 'how', 'to', 'what', 'is', 'are'];
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(word => word.length > 3 && !stopWords.includes(word));
}
```

**Integration in `src/utils/llm.ts`:**
```typescript
// Before generating embedding
const queryAnalysis = analyzeQuery(prompt);

// Use expanded queries for multi-query retrieval (optional)
const allResults = await Promise.all(
  queryAnalysis.expandedQueries.map(q =>
    generateEmbedding(q).then(emb =>
      similaritySearch({ inputEmbedding: emb, params: { limit: 5 } })
    )
  )
);

// Merge and deduplicate results
const mergedResults = mergeSearchResults(allResults);
```

**Testing:**
- [ ] Test query type classification accuracy
- [ ] Verify complexity detection works
- [ ] Check query expansions are relevant
- [ ] Test with various Aztec-specific queries

---

### Task 2.2: Hybrid Search with BM25
**Files:**
- `src/utils/hybridSearch.ts` (new)
- `src/utils/llm.ts` (update)

**Dependencies:** `@langchain/community` (already installed ✅)

**Purpose:** Combine semantic (vector) search with keyword (BM25) search

**Implementation:**
```typescript
import { BM25Retriever } from '@langchain/community/retrievers/bm25';
import { RecursiveCharacterTextSplitter } from '@langchain/textsplitters';

interface HybridSearchParams {
  query: string;
  embedding: number[];
  vectorWeight?: number;    // Default: 0.7
  keywordWeight?: number;   // Default: 0.3
  topK?: number;            // Default: 10
}

interface HybridSearchResult {
  documents: string[];
  metadatas: any[];
  scores: number[];  // Combined scores
}

// Cache BM25 retriever (rebuild when docs change)
let bm25RetrieverCache: BM25Retriever | null = null;
let lastBuildTime = 0;

async function getBM25Retriever(): Promise<BM25Retriever> {
  const now = Date.now();
  const oneHour = 60 * 60 * 1000;

  // Rebuild cache every hour or if null
  if (!bm25RetrieverCache || (now - lastBuildTime) > oneHour) {
    console.log('Building BM25 retriever from ChromaDB...');

    // Get all documents from ChromaDB
    const allDocs = await returnAllDocuments();

    const documents = allDocs.map(doc => ({
      pageContent: doc.text,
      metadata: { source: doc.source, title: doc.title }
    }));

    bm25RetrieverCache = BM25Retriever.fromDocuments(documents, {
      k: 20, // Retrieve more for later filtering
    });

    lastBuildTime = now;
    console.log(`BM25 retriever built with ${documents.length} documents`);
  }

  return bm25RetrieverCache;
}

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

  // Vector search
  const vectorResults = await similaritySearch({
    inputEmbedding: embedding,
    params: { limit: topK * 2 } // Get more for merging
  });

  // Keyword search
  const bm25 = await getBM25Retriever();
  const keywordDocs = await bm25.getRelevantDocuments(query);

  // Merge results with weighted scores
  const merged = mergeWithWeights(
    vectorResults,
    keywordDocs,
    vectorWeight,
    keywordWeight
  );

  // Sort by combined score and take top K
  const sorted = merged
    .sort((a, b) => b.score - a.score)
    .slice(0, topK);

  return {
    documents: sorted.map(r => r.document),
    metadatas: sorted.map(r => r.metadata),
    scores: sorted.map(r => r.score),
  };
}

function mergeWithWeights(
  vectorResults: SimilaritySearchResponse,
  keywordDocs: any[],
  vectorWeight: number,
  keywordWeight: number
): Array<{ document: string; metadata: any; score: number }> {
  const resultMap = new Map<string, any>();

  // Add vector results
  vectorResults.documents.forEach((doc, idx) => {
    const id = vectorResults.ids[idx];
    const distance = vectorResults.distances?.[0]?.[idx] || 0;
    const vectorScore = 1 - distance; // Convert distance to similarity

    resultMap.set(id, {
      document: doc,
      metadata: vectorResults.metadatas[0]?.[idx],
      vectorScore,
      keywordScore: 0,
      score: vectorScore * vectorWeight,
    });
  });

  // Add/update with keyword results
  keywordDocs.forEach((doc, idx) => {
    const id = doc.metadata.title || doc.pageContent.substring(0, 100);
    const keywordScore = 1 / (idx + 1); // Reciprocal rank

    if (resultMap.has(id)) {
      const existing = resultMap.get(id);
      existing.keywordScore = keywordScore;
      existing.score = (existing.vectorScore * vectorWeight) + (keywordScore * keywordWeight);
    } else {
      resultMap.set(id, {
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
```

**Integration in `src/utils/llm.ts`:**
```typescript
// Replace pure vector search with hybrid search
const results = await hybridSearch({
  query: prompt,
  embedding,
  vectorWeight: parseFloat(process.env.HYBRID_VECTOR_WEIGHT || '0.7'),
  keywordWeight: parseFloat(process.env.HYBRID_KEYWORD_WEIGHT || '0.3'),
  topK: env.AMOUNT_OF_DOCS,
});
```

**New Environment Variables:**
```bash
HYBRID_VECTOR_WEIGHT="0.7"      # Weight for semantic search
HYBRID_KEYWORD_WEIGHT="0.3"     # Weight for keyword search
ENABLE_HYBRID_SEARCH="true"     # Toggle hybrid vs pure vector
```

**Testing:**
- [ ] Verify BM25 retriever builds correctly
- [ ] Test with queries that should match keywords (exact terms)
- [ ] Compare results: hybrid vs pure vector
- [ ] Check performance impact (caching working?)

---

### Task 2.3: Relevance Filtering & Adaptive Retrieval
**File:** `src/utils/relevanceFiltering.ts` (new)

**Purpose:** Filter low-quality results and adjust retrieval count dynamically

**Implementation:**
```typescript
import { QueryAnalysis } from './queryAnalysis.js';

interface FilterConfig {
  minSimilarity: number;        // Default: 0.3 (distance < 0.7)
  maxResults: number;           // Default: 8
  minResults: number;           // Default: 2
}

export function filterByRelevance(
  results: SimilaritySearchResponse,
  config: FilterConfig
): SimilaritySearchResponse {
  const maxDistance = 1 - config.minSimilarity;

  const filtered = {
    ids: [] as string[],
    embeddings: [] as number[][],
    documents: [] as string[],
    metadatas: [[]] as any[][],
    distances: [[]] as number[][],
  };

  results.documents.forEach((doc, idx) => {
    const distance = results.distances?.[0]?.[idx];

    if (distance === undefined || distance <= maxDistance) {
      filtered.ids.push(results.ids[idx]);
      filtered.documents.push(doc);
      filtered.metadatas[0].push(results.metadatas[0]?.[idx]);

      if (results.embeddings?.[0]?.[idx]) {
        filtered.embeddings.push(results.embeddings[0][idx]);
      }
      if (distance !== undefined) {
        filtered.distances[0].push(distance);
      }
    }
  });

  // Ensure we have at least minResults
  if (filtered.documents.length < config.minResults) {
    console.warn(`Only ${filtered.documents.length} docs passed filter, threshold may be too strict`);
  }

  // Trim to maxResults
  if (filtered.documents.length > config.maxResults) {
    filtered.documents = filtered.documents.slice(0, config.maxResults);
    filtered.ids = filtered.ids.slice(0, config.maxResults);
    filtered.metadatas[0] = filtered.metadatas[0].slice(0, config.maxResults);
    if (filtered.distances[0].length > 0) {
      filtered.distances[0] = filtered.distances[0].slice(0, config.maxResults);
    }
  }

  return filtered;
}

export function getAdaptiveDocCount(analysis: QueryAnalysis): number {
  // Base count from query complexity
  let count = analysis.suggestedDocCount;

  // Adjust based on query type
  if (analysis.type === 'code_search') {
    count += 2; // Code searches benefit from more examples
  }

  // Respect environment limits
  const maxDocs = parseInt(process.env.RETRIEVAL_MAX_DOCS || '8');
  return Math.min(count, maxDocs);
}
```

**Integration:**
```typescript
// In src/utils/llm.ts
const queryAnalysis = analyzeQuery(prompt);
const docCount = getAdaptiveDocCount(queryAnalysis);

// Retrieve more than needed for filtering
let results = await hybridSearch({
  query: prompt,
  embedding,
  topK: docCount * 2,
});

// Apply relevance filtering
results = filterByRelevance(results, {
  minSimilarity: parseFloat(process.env.RETRIEVAL_MIN_SIMILARITY || '0.3'),
  maxResults: docCount,
  minResults: 2,
});
```

**New Environment Variables:**
```bash
RETRIEVAL_MIN_SIMILARITY="0.3"    # Minimum similarity score (0-1)
RETRIEVAL_MAX_DOCS="8"            # Maximum docs to retrieve
```

**Testing:**
- [ ] Verify low-quality results are filtered
- [ ] Test adaptive doc count with different query types
- [ ] Confirm minimum results requirement works
- [ ] Check edge cases (no results, all filtered)

---

## Phase 3: Re-ranking & Context Optimization (Days 6-8) 🟢 HIGH VALUE

**Priority:** HIGH - Significant impact on "wrong context"
**Status:** 🔲 Not Started

### Task 3.1: Cross-Encoder Re-ranking
**New File:** `src/utils/reranking.ts`

**Dependencies (Choose One):**
- Option A: `cohere-ai` (API-based, requires key)
- Option B: `@xenova/transformers` (local model, slower but free)

**Implementation - Option A (Cohere):**
```typescript
import { CohereClient } from 'cohere-ai';

const cohere = new CohereClient({
  token: process.env.COHERE_API_KEY,
});

interface RerankResult {
  documents: string[];
  metadatas: any[];
  scores: number[];
}

export async function rerankWithCohere(
  query: string,
  documents: string[],
  metadatas: any[],
  topN: number = 5
): Promise<RerankResult> {
  if (!process.env.COHERE_API_KEY) {
    console.warn('COHERE_API_KEY not set, skipping reranking');
    return { documents, metadatas, scores: [] };
  }

  try {
    const response = await cohere.rerank({
      model: 'rerank-english-v3.0',
      query,
      documents: documents.map((doc, idx) => ({
        text: doc,
        id: idx.toString(),
      })),
      topN,
      returnDocuments: false,
    });

    // Sort by relevance score
    const sorted = response.results
      .sort((a, b) => b.relevanceScore - a.relevanceScore)
      .map(r => ({
        document: documents[parseInt(r.index.toString())],
        metadata: metadatas[parseInt(r.index.toString())],
        score: r.relevanceScore,
      }));

    return {
      documents: sorted.map(r => r.document),
      metadatas: sorted.map(r => r.metadata),
      scores: sorted.map(r => r.score),
    };
  } catch (error) {
    console.error('Reranking failed:', error);
    return { documents, metadatas, scores: [] };
  }
}
```

**Implementation - Option B (Local Model):**
```typescript
import { pipeline } from '@xenova/transformers';

let reranker: any = null;

async function getReranker() {
  if (!reranker) {
    console.log('Loading cross-encoder model...');
    reranker = await pipeline(
      'text-classification',
      'cross-encoder/ms-marco-MiniLM-L-6-v2'
    );
  }
  return reranker;
}

export async function rerankWithLocalModel(
  query: string,
  documents: string[],
  metadatas: any[],
  topN: number = 5
): Promise<RerankResult> {
  try {
    const model = await getReranker();

    // Score each doc against query
    const scores = await Promise.all(
      documents.map(doc =>
        model(`${query} [SEP] ${doc}`, { topk: 1 })
      )
    );

    // Combine and sort
    const combined = documents.map((doc, idx) => ({
      document: doc,
      metadata: metadatas[idx],
      score: scores[idx][0].score,
    }));

    combined.sort((a, b) => b.score - a.score);
    const topResults = combined.slice(0, topN);

    return {
      documents: topResults.map(r => r.document),
      metadatas: topResults.map(r => r.metadata),
      scores: topResults.map(r => r.score),
    };
  } catch (error) {
    console.error('Local reranking failed:', error);
    return { documents, metadatas, scores: [] };
  }
}
```

**Integration:**
```typescript
// In src/utils/llm.ts
// After hybrid search and filtering

if (process.env.RERANK_ENABLED === 'true') {
  // Retrieve more docs initially (e.g., 10)
  // Then rerank to top N (e.g., 4-5)

  const reranked = process.env.COHERE_API_KEY
    ? await rerankWithCohere(prompt, results.documents, results.metadatas[0], env.AMOUNT_OF_DOCS)
    : await rerankWithLocalModel(prompt, results.documents, results.metadatas[0], env.AMOUNT_OF_DOCS);

  results.documents = reranked.documents;
  results.metadatas = [reranked.metadatas];
}
```

**New Environment Variables:**
```bash
RERANK_ENABLED="true"              # Enable/disable reranking
RERANK_METHOD="cohere"             # "cohere" or "local"
COHERE_API_KEY=""                  # If using Cohere
RERANK_TOP_N="5"                   # How many docs after reranking
```

**New Dependencies:**
```bash
# Option A
pnpm add cohere-ai

# Option B
pnpm add @xenova/transformers
```

**Testing:**
- [ ] Test both reranking methods work
- [ ] Compare reranked vs non-reranked results
- [ ] Measure latency impact
- [ ] Verify error handling when API unavailable

---

### Task 3.2: Context Window Optimization
**New File:** `src/utils/tokenCounting.ts`

**Dependencies:** `tiktoken` (OpenAI) or `@anthropic-ai/tokenizer`

**Purpose:** Ensure we don't exceed model context limits

**Implementation:**
```typescript
import { encoding_for_model } from 'tiktoken';

interface TokenCountResult {
  systemPromptTokens: number;
  chatHistoryTokens: number;
  contextTokens: number;
  totalTokens: number;
  maxTokens: number;
  remainingTokens: number;
}

export function countTokens(
  text: string,
  model: string = 'gpt-4'
): number {
  try {
    const enc = encoding_for_model(model as any);
    const tokens = enc.encode(text);
    enc.free();
    return tokens.length;
  } catch {
    // Fallback: rough estimate
    return Math.ceil(text.length / 4);
  }
}

export function analyzeTokenUsage(
  systemPrompt: string,
  chatHistory: string,
  context: string,
  modelMaxTokens: number = 128000 // GPT-4 default
): TokenCountResult {
  const systemPromptTokens = countTokens(systemPrompt);
  const chatHistoryTokens = countTokens(chatHistory);
  const contextTokens = countTokens(context);
  const totalTokens = systemPromptTokens + chatHistoryTokens + contextTokens;

  // Reserve tokens for response
  const maxResponseTokens = parseInt(process.env.MAX_RESPONSE_TOKENS || '8500');
  const maxTokens = modelMaxTokens - maxResponseTokens - 500; // 500 buffer

  return {
    systemPromptTokens,
    chatHistoryTokens,
    contextTokens,
    totalTokens,
    maxTokens,
    remainingTokens: maxTokens - totalTokens,
  };
}

export function optimizeContext(
  documents: string[],
  metadatas: any[],
  maxContextTokens: number
): { documents: string[]; metadatas: any[] } {
  const optimized: string[] = [];
  const optimizedMeta: any[] = [];
  let totalTokens = 0;

  // Always include most relevant (first) document
  if (documents.length > 0) {
    optimized.push(documents[0]);
    optimizedMeta.push(metadatas[0]);
    totalTokens = countTokens(documents[0]);
  }

  // Add remaining docs while under limit
  for (let i = 1; i < documents.length; i++) {
    const docTokens = countTokens(documents[i]);

    if (totalTokens + docTokens <= maxContextTokens) {
      optimized.push(documents[i]);
      optimizedMeta.push(metadatas[i]);
      totalTokens += docTokens;
    } else {
      // Try to fit a truncated version
      const availableTokens = maxContextTokens - totalTokens;
      if (availableTokens > 500) { // Only if we have decent space
        const truncated = truncateToTokens(documents[i], availableTokens - 50);
        optimized.push(truncated + '\n... [truncated]');
        optimizedMeta.push(metadatas[i]);
      }
      break; // Stop adding docs
    }
  }

  console.log(`Context optimized: ${documents.length} → ${optimized.length} docs (${totalTokens} tokens)`);

  return {
    documents: optimized,
    metadatas: optimizedMeta,
  };
}

function truncateToTokens(text: string, maxTokens: number): string {
  // Binary search for character count that fits token limit
  let low = 0;
  let high = text.length;

  while (low < high) {
    const mid = Math.floor((low + high + 1) / 2);
    const tokens = countTokens(text.substring(0, mid));

    if (tokens <= maxTokens) {
      low = mid;
    } else {
      high = mid - 1;
    }
  }

  return text.substring(0, low);
}
```

**Integration:**
```typescript
// In src/utils/llm.ts
import { analyzeTokenUsage, optimizeContext } from './tokenCounting.js';

// Before calling LLM
const contextString = formatRetrievedContext(results);
const chatHistoryString = formatChatHistory(chatHistory);

const tokenAnalysis = analyzeTokenUsage(
  SYSTEM_PROMPT,
  chatHistoryString,
  contextString,
  128000 // Adjust based on model
);

if (tokenAnalysis.remainingTokens < 0) {
  console.warn(`Context exceeds limit by ${-tokenAnalysis.remainingTokens} tokens, optimizing...`);

  const optimized = optimizeContext(
    results.documents,
    results.metadatas[0],
    tokenAnalysis.maxTokens - tokenAnalysis.systemPromptTokens - tokenAnalysis.chatHistoryTokens
  );

  results.documents = optimized.documents;
  results.metadatas = [optimized.metadatas];
}
```

**New Dependencies:**
```bash
pnpm add tiktoken
```

**Testing:**
- [ ] Verify token counting is accurate
- [ ] Test with contexts that exceed limits
- [ ] Confirm optimization preserves most relevant docs
- [ ] Check truncation works correctly

---

### Task 3.3: Source Diversity & Freshness
**New File:** `src/utils/sourceDiversity.ts`

**Purpose:** Ensure variety in sources and boost recent content

**Implementation:**
```typescript
interface DiversityConfig {
  maxPerSource?: number;        // Default: 2
  maxPerFileType?: number;       // Default: 3
  requireTypes?: string[];       // e.g., ['docs', 'example']
  freshnessBias?: number;        // Default: 0.2 (20% boost for recent)
}

export function ensureSourceDiversity(
  documents: string[],
  metadatas: any[],
  distances: number[],
  config: DiversityConfig = {}
): { documents: string[]; metadatas: any[]; distances: number[] } {
  const {
    maxPerSource = 2,
    maxPerFileType = 3,
    requireTypes = [],
    freshnessBias = 0.2
  } = config;

  // Track counts
  const sourceCount = new Map<string, number>();
  const fileTypeCount = new Map<string, number>();
  const typesSeen = new Set<string>();

  const filtered: Array<{ doc: string; meta: any; dist: number }> = [];

  // First pass: ensure required types
  for (let i = 0; i < documents.length; i++) {
    const meta = metadatas[i];
    const docType = getDocumentType(meta);

    if (requireTypes.length > 0 && requireTypes.includes(docType)) {
      if (!typesSeen.has(docType)) {
        filtered.push({ doc: documents[i], meta, dist: distances[i] });
        typesSeen.add(docType);
        updateCounts(meta, sourceCount, fileTypeCount);
      }
    }
  }

  // Second pass: add diverse docs
  for (let i = 0; i < documents.length; i++) {
    if (filtered.length >= documents.length) break;

    const meta = metadatas[i];
    const source = getSource(meta);
    const fileType = meta.fileType || 'unknown';

    // Skip if already included
    if (filtered.some(f => f.doc === documents[i])) continue;

    // Check diversity constraints
    const sourceOk = !sourceCount.has(source) || sourceCount.get(source)! < maxPerSource;
    const fileTypeOk = !fileTypeCount.has(fileType) || fileTypeCount.get(fileType)! < maxPerFileType;

    if (sourceOk && fileTypeOk) {
      filtered.push({ doc: documents[i], meta, dist: distances[i] });
      updateCounts(meta, sourceCount, fileTypeCount);
    }
  }

  // Apply freshness boosting
  const now = Date.now();
  const thirtyDays = 30 * 24 * 60 * 60 * 1000;

  filtered.forEach(item => {
    const age = now - (item.meta.date || 0);
    if (age < thirtyDays) {
      // Reduce distance (improve rank) for recent docs
      item.dist = item.dist * (1 - freshnessBias);
    }
  });

  // Re-sort by adjusted distance
  filtered.sort((a, b) => a.dist - b.dist);

  return {
    documents: filtered.map(f => f.doc),
    metadatas: filtered.map(f => f.meta),
    distances: filtered.map(f => f.dist),
  };
}

function getDocumentType(metadata: any): string {
  if (metadata.isDocumentation) return 'docs';
  if (metadata.isExample) return 'example';
  if (metadata.fileType === 'typescript' || metadata.fileType === 'javascript') return 'code';
  return 'other';
}

function getSource(metadata: any): string {
  // Extract repo/folder from link
  const link = metadata.link || '';
  const match = link.match(/github\.com\/[^\/]+\/([^\/]+)/);
  return match ? match[1] : 'unknown';
}

function updateCounts(
  metadata: any,
  sourceCount: Map<string, number>,
  fileTypeCount: Map<string, number>
) {
  const source = getSource(metadata);
  const fileType = metadata.fileType || 'unknown';

  sourceCount.set(source, (sourceCount.get(source) || 0) + 1);
  fileTypeCount.set(fileType, (fileTypeCount.get(fileType) || 0) + 1);
}
```

**Integration:**
```typescript
// In src/utils/llm.ts
// After reranking

results = ensureSourceDiversity(
  results.documents,
  results.metadatas[0],
  results.distances?.[0] || [],
  {
    maxPerSource: 2,
    requireTypes: queryAnalysis.type === 'how_to' ? ['example'] : [],
    freshnessBias: 0.2,
  }
);
```

**Testing:**
- [ ] Verify no single source dominates results
- [ ] Check required types are included
- [ ] Confirm freshness boosting works
- [ ] Test with edge cases (all from one source)

---

## Phase 4: Response Quality & Validation (Days 9-11)

**Priority:** MEDIUM - Polish and reliability
**Status:** 🔲 Not Started

### Task 4.1: Citation Validation
**New File:** `src/utils/citationValidation.ts`

**Purpose:** Ensure LLM only cites sources from retrieved context

**Implementation:**
```typescript
interface CitationCheck {
  valid: boolean;
  validCitations: string[];
  invalidCitations: string[];
  uncitedSources: string[];
}

export function validateCitations(
  response: string,
  retrievedSources: Array<{ link: string }>
): CitationCheck {
  // Extract URLs from response
  const urlRegex = /https?:\/\/[^\s\)]+/g;
  const citedUrls = response.match(urlRegex) || [];

  // Extract valid source URLs
  const validUrls = retrievedSources.map(s => s.link);

  // Check each citation
  const validCitations: string[] = [];
  const invalidCitations: string[] = [];

  citedUrls.forEach(url => {
    const normalized = normalizeUrl(url);
    const isValid = validUrls.some(validUrl =>
      normalizeUrl(validUrl) === normalized
    );

    if (isValid) {
      validCitations.push(url);
    } else {
      invalidCitations.push(url);
    }
  });

  // Find sources that weren't cited
  const citedNormalized = citedUrls.map(normalizeUrl);
  const uncitedSources = validUrls.filter(url =>
    !citedNormalized.includes(normalizeUrl(url))
  );

  return {
    valid: invalidCitations.length === 0,
    validCitations,
    invalidCitations,
    uncitedSources,
  };
}

function normalizeUrl(url: string): string {
  // Remove trailing slashes, fragments, query params
  return url
    .replace(/[#?].*$/, '')
    .replace(/\/$/, '')
    .toLowerCase();
}
```

**Integration:**
```typescript
// In src/handlers/messages.ts
// After receiving LLM response

const citationCheck = validateCitations(
  ragResponse.answer,
  ragResponse.sources
);

if (!citationCheck.valid) {
  console.warn(`Invalid citations detected:`, citationCheck.invalidCitations);

  // Option 1: Regenerate
  // Option 2: Append warning
  // Option 3: Log for monitoring
}
```

**Testing:**
- [ ] Test with responses containing valid citations
- [ ] Test with hallucinated URLs
- [ ] Verify URL normalization works
- [ ] Check uncited sources detection

---

### Task 4.2: Enhanced System Prompts
**File:** `src/config/prompts.ts`

**Purpose:** Add instructions for handling conflicts, uncertainty, version awareness

**Implementation:**
```typescript
export const ENHANCED_SYSTEM_PROMPT = `
${SYSTEM_PROMPT} // Existing prompt

## Additional Guidelines

### Version Awareness
- When discussing Aztec features, note the version if multiple versions exist in context
- Prefer information from the most recent version unless specifically asked about older versions
- If documentation conflicts between versions, mention this and recommend using the latest version

### Handling Uncertainty
- If the provided context doesn't fully answer the question, say so explicitly
- Indicate your confidence level when appropriate:
  - High confidence: Clear information in multiple sources
  - Medium confidence: Information found but limited or single source
  - Low confidence: Inferring from limited context
- Say "I don't know" rather than guessing when information is not in the provided context

### Conflicting Information
- If sources contradict each other:
  1. Note the conflict explicitly
  2. Prefer more recent/official sources
  3. Recommend checking the official docs for confirmation

### Citation Requirements
- ONLY cite URLs that appear in the provided source documents
- Do not generate or guess URLs
- If you reference information, cite its source
`;
```

**Testing:**
- [ ] Test responses with conflicting information
- [ ] Verify confidence indicators appear when appropriate
- [ ] Check version awareness in responses
- [ ] Confirm "I don't know" responses when appropriate

---

## Configuration Changes

### New Environment Variables

Add to `.env-example` and update `src/env.ts`:

```bash
# Retrieval Configuration
RETRIEVAL_MIN_SIMILARITY="0.3"        # Minimum similarity (0-1)
RETRIEVAL_MAX_DOCS="8"                # Maximum docs to retrieve
VERSION_BOOST_FACTOR="1.5"            # Boost current version docs

# Hybrid Search
ENABLE_HYBRID_SEARCH="true"           # Enable hybrid search
HYBRID_VECTOR_WEIGHT="0.7"            # Vector search weight
HYBRID_KEYWORD_WEIGHT="0.3"           # Keyword search weight

# Re-ranking
RERANK_ENABLED="true"                 # Enable re-ranking
RERANK_METHOD="cohere"                # "cohere" or "local"
RERANK_TOP_N="5"                      # Docs after reranking
COHERE_API_KEY=""                     # If using Cohere

# Query Analysis
ENABLE_QUERY_EXPANSION="true"         # Expand queries with synonyms
MAX_QUERY_EXPANSIONS="3"              # Max query variations
```

**Update `src/env.ts`:**
```typescript
export const envSchema = z.object({
  // ... existing fields

  // Retrieval Configuration
  RETRIEVAL_MIN_SIMILARITY: z.string().default("0.3").transform(val => parseFloat(val)),
  RETRIEVAL_MAX_DOCS: z.string().default("8").transform(val => parseInt(val, 10)),
  VERSION_BOOST_FACTOR: z.string().default("1.5").transform(val => parseFloat(val)),

  // Hybrid Search
  ENABLE_HYBRID_SEARCH: z.string().default("true").transform(val => val === "true"),
  HYBRID_VECTOR_WEIGHT: z.string().default("0.7").transform(val => parseFloat(val)),
  HYBRID_KEYWORD_WEIGHT: z.string().default("0.3").transform(val => parseFloat(val)),

  // Re-ranking
  RERANK_ENABLED: z.string().default("false").transform(val => val === "true"),
  RERANK_METHOD: z.enum(["cohere", "local"]).default("cohere"),
  RERANK_TOP_N: z.string().default("5").transform(val => parseInt(val, 10)),
  COHERE_API_KEY: z.string().optional(),

  // Query Analysis
  ENABLE_QUERY_EXPANSION: z.string().default("true").transform(val => val === "true"),
  MAX_QUERY_EXPANSIONS: z.string().default("3").transform(val => parseInt(val, 10)),
});
```

---

## Testing Strategy

### Unit Tests
Create `tests/` directory with:
- `queryAnalysis.test.ts`
- `hybridSearch.test.ts`
- `versionFiltering.test.ts`
- `citationValidation.test.ts`

### Integration Tests
- Full RAG pipeline with test queries
- Compare old vs new results
- Measure response quality

### Performance Tests
- Measure latency impact of each feature
- Token counting overhead
- BM25 caching effectiveness

### Regression Tests
Create test cases for known issues:
- "Deploy an Aztec contract" (should return latest docs)
- "What is Aztec.nr?" (should find correct library docs)
- "How to use PXE?" (should include code examples)

---

## Rollout Plan

### Week 1: Foundation + Basic Retrieval
**Days 1-5:**
- [ ] Phase 1: Foundation Fixes (complete and test)
- [ ] Phase 2: Retrieval Intelligence (complete and test)
- [ ] Deploy to staging/test environment
- [ ] Gather initial feedback
- [ ] **Decision Point:** Go/No-Go for Phase 3

### Week 2: Advanced Features + Polish
**Days 6-11:**
- [ ] Phase 3: Re-ranking & Context Optimization
- [ ] Phase 4: Response Quality & Validation
- [ ] Full integration testing
- [ ] Performance optimization
- [ ] **Decision Point:** Production deployment

### Post-Deployment
- Monitor metrics (thumbs up/down, query success rate)
- Collect user feedback
- Iterate on configuration (weights, thresholds)

---

## Success Metrics

Track before and after:

### Quantitative
- **Thumbs up ratio:** Target +20% improvement
- **Average response time:** Keep under 5 seconds
- **Query success rate:** % of queries that retrieve relevant docs
- **Citation accuracy:** % of responses with only valid citations

### Qualitative (User Feedback)
- Outdated information complaints: Target -70% reduction
- "Can't find" complaints: Target -50% reduction
- Wrong context complaints: Target -60% reduction

### Technical
- Token usage efficiency
- Context window utilization
- Cache hit rates (BM25, reranker)
- Error rates

---

## Risk Mitigation

### Technical Risks
| Risk | Impact | Mitigation |
|------|--------|-----------|
| Performance degradation | High | Feature flags, caching, parallel processing |
| API costs (Cohere/OpenAI) | Medium | Local model fallback, rate limiting |
| Breaking changes | High | Feature flags, gradual rollout, rollback plan |
| Data loss during re-training | Critical | Snapshot before re-train, backup ChromaDB |

### Rollback Plan
- All features have environment variable toggles
- Can disable individually without code changes
- Keep old retrieval logic as fallback
- Database snapshots before major changes

---

## Maintenance

### Weekly
- Monitor metrics dashboard
- Review error logs
- Check API usage/costs

### Monthly
- Review user feedback
- Tune configuration parameters
- Update synonym mappings
- Retrain with latest docs

### Quarterly
- Evaluate new RAG techniques
- Consider model upgrades
- Performance audit

---

## Next Steps

1. **Review and approve this plan**
2. **Set up development branch:** `feature/rag-optimization`
3. **Create tracking issues** for each phase
4. **Start with Phase 1, Task 1.1** - Re-enable markdown chunking
5. **Daily standups** to track progress and blockers

---

## Notes & Decisions

_Document key decisions and changes as implementation progresses_

**[Date]** - Decision:
**[Date]** - Blocker:
**[Date]** - Change:
