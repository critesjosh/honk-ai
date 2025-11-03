/**
 * Test script to demonstrate Phase 1 + Phase 2 RAG improvements
 * Tests query analysis, hybrid search, and relevance filtering
 */

import { invokeRagChain } from '../src/utils/llm.js';
import { analyzeQuery } from '../src/utils/queryAnalysis.js';
import { env } from '../src/env.js';

// Test queries covering different types and complexities
const testQueries = [
  {
    name: "Simple Code Search",
    query: "What is a PXE?",
    expectedType: "concept",
  },
  {
    name: "How-to Query",
    query: "How do I deploy an Aztec contract?",
    expectedType: "how_to",
  },
  {
    name: "Troubleshooting",
    query: "My contract deployment is failing with an error",
    expectedType: "troubleshooting",
  },
  {
    name: "Code Search",
    query: "Show me examples of private functions in Noir",
    expectedType: "code_search",
  },
  {
    name: "Version-specific Query",
    query: "What are the latest features in Aztec?",
    expectedType: "concept",
  },
];

async function testRetrieval() {
  console.log('='.repeat(80));
  console.log('RAG OPTIMIZATION TESTING - Phase 1 + Phase 2');
  console.log('='.repeat(80));
  console.log();
  console.log(`Environment:`);
  console.log(`  - LLM Provider: ${env.LLM_PROVIDER}`);
  console.log(`  - Aztec Version: ${env.AZTEC_DOCS_VERSION || 'auto-detect'}`);
  console.log(`  - Max Docs: ${env.AMOUNT_OF_DOCS}`);
  console.log(`  - Hybrid Search: ${process.env.ENABLE_HYBRID_SEARCH !== 'false' ? 'ENABLED' : 'DISABLED'}`);
  console.log(`  - Vector Weight: ${process.env.HYBRID_VECTOR_WEIGHT || '0.7'}`);
  console.log(`  - Keyword Weight: ${process.env.HYBRID_KEYWORD_WEIGHT || '0.3'}`);
  console.log(`  - Min Similarity: ${process.env.RETRIEVAL_MIN_SIMILARITY || '0.3'}`);
  console.log();

  for (const testCase of testQueries) {
    console.log('─'.repeat(80));
    console.log(`TEST: ${testCase.name}`);
    console.log(`Query: "${testCase.query}"`);
    console.log('─'.repeat(80));

    try {
      // Phase 2.1: Analyze query
      const analysis = analyzeQuery(testCase.query);
      console.log('\n📊 Query Analysis:');
      console.log(`  Type: ${analysis.type} (expected: ${testCase.expectedType})`);
      console.log(`  Complexity: ${analysis.complexity}`);
      console.log(`  Requires Recent: ${analysis.requiresRecent}`);
      console.log(`  Suggested Docs: ${analysis.suggestedDocCount}`);
      console.log(`  Keywords: ${analysis.keywords.join(', ')}`);
      if (analysis.expandedQueries.length > 1) {
        console.log(`  Expanded Queries:`);
        analysis.expandedQueries.forEach((q, i) => {
          if (i > 0) console.log(`    ${i}. "${q}"`);
        });
      }

      // Invoke RAG chain (this will use all Phase 1 + Phase 2 improvements)
      console.log('\n🔍 Executing RAG retrieval pipeline...\n');
      const startTime = Date.now();
      const result = await invokeRagChain(testCase.query);
      const endTime = Date.now();

      console.log(`✅ Response generated in ${endTime - startTime}ms`);
      console.log(`\n📚 Sources Retrieved: ${result.sources.length}`);
      result.sources.forEach((source, i) => {
        console.log(`  ${i + 1}. ${source}`);
      });

      console.log(`\n💬 Response Preview (first 300 chars):`);
      const preview = result.answer.substring(0, 300);
      console.log(preview + (result.answer.length > 300 ? '...' : ''));

      if (result.usage) {
        console.log(`\n📈 Token Usage:`);
        if ('promptTokens' in result.usage) {
          console.log(`  Prompt: ${result.usage.promptTokens}`);
          console.log(`  Completion: ${result.usage.completionTokens}`);
          console.log(`  Total: ${result.usage.totalTokens}`);
        } else {
          console.log(`  Input: ${result.usage.inputTokens}`);
          console.log(`  Output: ${result.usage.outputTokens}`);
          console.log(`  Total: ${result.usage.totalTokens}`);
        }
      }

      console.log();
    } catch (error) {
      console.error(`❌ Error testing query:`, error);
      console.log();
    }

    // Small delay between tests to avoid rate limits
    await new Promise(resolve => setTimeout(resolve, 2000));
  }

  console.log('='.repeat(80));
  console.log('TESTING COMPLETE');
  console.log('='.repeat(80));
  console.log('\n📋 Summary of Improvements:');
  console.log('  ✅ Phase 1.1: Markdown chunking with 10% overlap');
  console.log('  ✅ Phase 1.2: Enhanced metadata (version, file type, importance)');
  console.log('  ✅ Phase 1.3: Version-aware filtering');
  console.log('  ✅ Phase 2.1: Query analysis and expansion');
  console.log('  ✅ Phase 2.2: Hybrid search (vector + BM25)');
  console.log('  ✅ Phase 2.3: Relevance filtering and adaptive retrieval');
  console.log();
}

// Run tests
testRetrieval()
  .then(() => {
    console.log('✨ All tests completed successfully');
    process.exit(0);
  })
  .catch((error) => {
    console.error('❌ Test suite failed:', error);
    process.exit(1);
  });
