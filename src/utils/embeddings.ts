import OpenAI from "openai";
import { env } from "../env.js";

const openai = new OpenAI({
  apiKey: env.OPEN_AI_API,
});

export async function generateEmbedding(text: string): Promise<number[]> {
  try {
    const response = await openai.embeddings.create({
      model: env.EMBEDDING_MODEL,
      input: text,
    });

    return response.data[0].embedding;
  } catch (error) {
    console.error("Error generating embedding:", error);
    throw error;
  }
}

export default {
  generateEmbedding,
};