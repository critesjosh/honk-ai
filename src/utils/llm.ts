import { env } from "../env.js";
import { Conversation } from "../types/data.js";
import * as OpenAIModel from "./models/openai.js";
import * as AnthropicModel from "./models/anthropic.js";

function getProvider(): "openai" | "anthropic" {
  return (env.LLM_PROVIDER as "openai" | "anthropic") || "openai";
}

export async function generateResponse(system: string, human: string) {
  const provider = getProvider();
  if (provider === "openai") {
    return OpenAIModel.generateResponse(system, human);
  }
  return AnthropicModel.generateResponse(system, human);
}

export async function invokeRagChain(
  prompt: string,
  chatHistory?: Conversation,
): Promise<{
  answer: string;
  sources: string[];
  usage?: any;
  model?: string;
  provider?: "openai" | "anthropic";
}> {
  const provider = getProvider();
  if (provider === "openai") {
    return OpenAIModel.invokeRag(prompt, chatHistory);
  }
  return AnthropicModel.invokeRag(prompt, chatHistory);
}

export default {
  generateResponse,
  invokeRagChain,
};
