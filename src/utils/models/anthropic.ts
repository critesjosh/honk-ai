import Anthropic from "@anthropic-ai/sdk";
import { env } from "../../env.js";
import { similaritySearch } from "../chroma.js";
import { generateEmbedding } from "../embeddings.js";
import { Conversation } from "../../types/data.js";
import prompts from "../../config/prompts.js";

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
    const promptEmbedding = await generateEmbedding(prompt);

    const similarDocuments = await similaritySearch({
      inputEmbedding: promptEmbedding,
      params: { limit: env.AMOUNT_OF_DOCS },
    });

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
