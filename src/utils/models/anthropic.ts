import Anthropic from "@anthropic-ai/sdk";
import { env } from "../../env.js";
import { similaritySearch } from "../chroma.js";
import { generateEmbedding } from "../embeddings.js";
import { Conversation } from "../../types/data.js";
import prompts from "../../config/prompts.js";
import { filterAndBoostByVersion } from "../versionFiltering.js";
import { analyzeQuery, getAdaptiveDocCount } from "../queryAnalysis.js";
import { hybridSearch } from "../hybridSearch.js";
import { filterByRelevance, logFilteringStats } from "../relevanceFiltering.js";

const anthropic = new Anthropic({ apiKey: env.ANTHROPIC_API });

export async function generateResponseWithMeta(
  system: string,
  human: string,
  history?: Conversation,
): Promise<{
  text: string;
  usage?: { inputTokens?: number; outputTokens?: number; totalTokens?: number };
  model: string;
}> {
  const historyMessages = (history || []).map((h) => ({
    role: h.who === "assistant" ? ("assistant" as const) : ("user" as const),
    content: h.content,
  }));

  const response = await anthropic.messages.create({
    model: env.ANTHROPIC_MODEL,
    max_tokens: env.MAX_RESPONSE_TOKENS,
    temperature: 0.5,
    system,
    messages: [...historyMessages, { role: "user", content: human }],
    ...(env.ANTHROPIC_THINKING && {
      thinking: {
        type: "enabled",
        budget_tokens: env.MAX_RESPONSE_TOKENS,
      },
    }),
  });

  const textContent = response.content.find((block) => block.type === "text");
  const text = textContent?.text || "";

  const usage = (response as any).usage
    ? {
        inputTokens: (response as any).usage.input_tokens,
        outputTokens: (response as any).usage.output_tokens,
        totalTokens:
          (response as any).usage.input_tokens +
          (response as any).usage.output_tokens,
      }
    : undefined;

  return { text, usage, model: env.ANTHROPIC_MODEL };
}

export async function generateResponse(
  system: string,
  human: string,
  history?: Conversation,
) {
  const { text } = await generateResponseWithMeta(system, human, history);
  return text;
}

export async function invokeRag(
  prompt: string,
  chatHistory?: Conversation,
): Promise<{
  answer: string;
  sources: string[];
  usage?: { inputTokens?: number; outputTokens?: number; totalTokens?: number };
  model?: string;
  provider: "anthropic";
}> {
  try {
    // Phase 2.1: Analyze query to understand intent and complexity
    const queryAnalysis = analyzeQuery(prompt);
    const adaptiveDocCount = getAdaptiveDocCount(queryAnalysis, env.AMOUNT_OF_DOCS);

    console.log(
      `Query analysis: type=${queryAnalysis.type}, complexity=${queryAnalysis.complexity}, ` +
      `suggestedDocs=${adaptiveDocCount}`
    );

    // Generate embedding for the query
    console.time('⏱️  embedding-generation');
    const promptEmbedding = await generateEmbedding(prompt);
    console.timeEnd('⏱️  embedding-generation');

    // Phase 2.2: Use hybrid search if enabled, otherwise fall back to vector search
    const enableHybrid = process.env.ENABLE_HYBRID_SEARCH !== 'false'; // Default: enabled
    let similarDocuments;

    console.time('⏱️  retrieval');
    if (enableHybrid) {
      // Retrieve more docs initially for filtering (reduced from * 2 to + 3)
      const retrievalCount = adaptiveDocCount + 3;

      try {
        const hybridResults = await hybridSearch({
          query: prompt,
          embedding: promptEmbedding,
          vectorWeight: parseFloat(process.env.HYBRID_VECTOR_WEIGHT || '0.7'),
          keywordWeight: parseFloat(process.env.HYBRID_KEYWORD_WEIGHT || '0.3'),
          topK: retrievalCount,
        });

        // Convert hybrid results to SimilaritySearchResponse format
        similarDocuments = {
          ids: hybridResults.ids,
          embeddings: [],
          documents: hybridResults.documents,
          metadatas: [hybridResults.metadatas],
          distances: hybridResults.distances || [[]]
        };
      } catch (error) {
        console.warn('Hybrid search failed, falling back to vector search:', error);
        similarDocuments = await similaritySearch({
          inputEmbedding: promptEmbedding,
          params: { limit: adaptiveDocCount + 3 },
        });
      }
    } else {
      similarDocuments = await similaritySearch({
        inputEmbedding: promptEmbedding,
        params: { limit: adaptiveDocCount + 3 },
      });
    }
    console.timeEnd('⏱️  retrieval');

    // Phase 2.3: Apply relevance filtering
    const originalCount = similarDocuments.documents.length;
    similarDocuments = filterByRelevance(similarDocuments, {
      minSimilarity: parseFloat(process.env.RETRIEVAL_MIN_SIMILARITY || '0.3'),
      maxResults: adaptiveDocCount,
      minResults: 2,
    });

    logFilteringStats(
      { count: originalCount, type: 'retrieved' },
      { count: similarDocuments.documents.length },
      {
        minSimilarity: parseFloat(process.env.RETRIEVAL_MIN_SIMILARITY || '0.3'),
        maxResults: adaptiveDocCount,
        minResults: 2,
      }
    );

    // Phase 1.3: Apply version filtering to prioritize current version docs
    if (env.AZTEC_DOCS_VERSION) {
      similarDocuments = filterAndBoostByVersion(similarDocuments, {
        currentVersion: env.AZTEC_DOCS_VERSION,
        boostFactor: parseFloat(process.env.VERSION_BOOST_FACTOR || '1.5'),
        penaltyFactor: 0.5,
      });
    }

    if (similarDocuments.documents.length === 0) {
      return {
        answer: "I couldn't find relevant information to answer your question.",
        sources: [],
        provider: "anthropic",
      };
    }

    const formattedContext = similarDocuments.documents
      .map((doc, index) => {
        const metadata = similarDocuments.metadatas?.[0]?.[index] as Record<
          string,
          string | number | boolean
        > | null;
        const source = metadata?.link || "Unknown source";
        return `|s----|\nSource: ${source}\n${doc}\n|e----|`;
      })
      .join("\n\n");

    const promptConfig = prompts.getHonkPrompt(formattedContext, prompt);

    console.time('⏱️  llm-generation');
    const resp = await generateResponseWithMeta(
      promptConfig.system,
      promptConfig.human,
      chatHistory,
    );
    console.timeEnd('⏱️  llm-generation');

    const sources: string[] = [];
    if (similarDocuments.metadatas?.[0]) {
      similarDocuments.metadatas[0].forEach((metadata) => {
        if (metadata?.link && typeof metadata.link === "string") {
          sources.push(metadata.link);
        }
      });
    }

    return {
      answer: resp.text,
      sources: [...new Set(sources)],
      usage: resp.usage,
      model: resp.model,
      provider: "anthropic",
    };
  } catch (error) {
    console.error("Error in Anthropic RAG chain:", error);
    return {
      answer: "Sorry, I encountered an error while processing your request.",
      sources: [],
      provider: "anthropic",
    };
  }
}

export default { generateResponse, generateResponseWithMeta, invokeRag };
