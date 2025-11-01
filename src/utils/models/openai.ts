import OpenAI from "openai";
import { env } from "../../env.js";
import { similaritySearch } from "../chroma.js";
import { generateEmbedding } from "../embeddings.js";
import { Conversation } from "../../types/data.js";
import prompts from "../../config/prompts.js";
import { filterAndBoostByVersion } from "../versionFiltering.js";

const openai = new OpenAI({ apiKey: env.OPEN_AI_API });

export async function generateResponseWithMeta(
  system: string,
  human: string,
  history?: Conversation,
): Promise<{
  text: string;
  usage?: {
    promptTokens?: number;
    completionTokens?: number;
    totalTokens?: number;
  };
  model: string;
}> {
  const historyMessages = (history || []).map((h) => ({
    role: h.who === "assistant" ? ("assistant" as const) : ("user" as const),
    content: h.content,
  }));

  const completion = await openai.chat.completions.create({
    model: env.OPENAI_MODEL,
    max_completion_tokens: env.MAX_RESPONSE_TOKENS,
    reasoning_effort: "medium",
    // temperature: 0.5, Only 1 is supported for gpt-5
    messages: [
      { role: "system", content: system },
      ...historyMessages,
      { role: "user", content: human },
    ],
  });

  const content = completion.choices?.[0]?.message?.content || "";
  const text =
    typeof content === "string" ? content : (content as any)?.[0]?.text || "";
  const usage = completion.usage
    ? {
        promptTokens: (completion.usage as any).prompt_tokens,
        completionTokens: (completion.usage as any).completion_tokens,
        totalTokens: (completion.usage as any).total_tokens,
      }
    : undefined;
  const model = completion.model || env.OPENAI_MODEL;

  return { text, usage, model };
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
  usage?: {
    promptTokens?: number;
    completionTokens?: number;
    totalTokens?: number;
  };
  model?: string;
  provider: "openai";
}> {
  try {
    const promptEmbedding = await generateEmbedding(prompt);

    let similarDocuments = await similaritySearch({
      inputEmbedding: promptEmbedding,
      params: { limit: env.AMOUNT_OF_DOCS },
    });

    // Apply version filtering to prioritize current version docs
    if (env.AZTEC_DOCS_VERSION) {
      similarDocuments = filterAndBoostByVersion(similarDocuments, {
        currentVersion: env.AZTEC_DOCS_VERSION,
        boostFactor: 1.5,
        penaltyFactor: 0.5,
      });
    }

    if (similarDocuments.documents.length === 0) {
      return {
        answer: "I couldn't find relevant information to answer your question.",
        sources: [],
        provider: "openai",
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

    const resp = await generateResponseWithMeta(
      promptConfig.system,
      promptConfig.human,
      chatHistory,
    );

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
      provider: "openai",
    };
  } catch (error) {
    console.error("Error in OpenAI RAG chain:", error);
    return {
      answer: "Sorry, I encountered an error while processing your request.",
      sources: [],
      provider: "openai",
    };
  }
}

export default { generateResponse, generateResponseWithMeta, invokeRag };
