import { ChromaClient, IncludeEnum } from "chromadb";
import { OpenAIEmbeddingFunction } from "@chroma-core/openai";
import { SimilaritySearchResponse } from "../types/data.js";
import { env } from "../env.js";

let chromaClient: ChromaClient | null = null;
let knowledgeCollection: Awaited<
  ReturnType<ChromaClient["getOrCreateCollection"]>
> | null = null;

const embeddingFunction = new OpenAIEmbeddingFunction({
  apiKey: env.OPEN_AI_API,
  modelName: env.EMBEDDING_MODEL,
});

async function getChromaInstance() {
  if (!chromaClient) {
    chromaClient = new ChromaClient({
      host: env.CHROMADB_URL_PATH,
      port: env.CHROMADB_URL_PORT,
      ssl: env.CHROMADB_URL_SSL,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Basic ${env.CHROMADB_AUTH || ""}`,
      },
    });
  }
  return chromaClient;
}

async function getKnowledgeCollection() {
  if (!knowledgeCollection) {
    const client = await getChromaInstance();
    knowledgeCollection = await client.getOrCreateCollection({
      name: env.CHROMADB_KNOWLEDGEBASE_ID,
      metadata: { "hnsw:space": "cosine" },
      embeddingFunction: embeddingFunction,
    });
  }
  return knowledgeCollection;
}

export async function UpsertDocument(
  id: string,
  document: string,
  source: string,
): Promise<boolean> {
  if (!id || !document || !source) return false;

  if (typeof id !== "string") return false;
  if (typeof document !== "string") return false;
  if (typeof source !== "string") return false;

  try {
    // Store document without prefix/suffix to save tokens
    const collection = await getKnowledgeCollection();

    await collection.upsert({
      documents: [document],
      ids: [id],
      metadatas: [{ link: source, date: Date.now() }],
    });

    return true;
  } catch (error) {
    console.error(error);
    return false;
  }
}

export async function similaritySearch({
  inputEmbedding,
  params = { limit: 4 },
}: {
  inputEmbedding: number[];
  params?: {
    limit?: number;
  };
}): Promise<SimilaritySearchResponse> {
  const collection = await getKnowledgeCollection();

  const queryRes = await collection.query({
    queryEmbeddings: [inputEmbedding],
    nResults: params.limit,
    include: [
      "embeddings",
      "documents",
      "metadatas",
      "distances",
    ] as IncludeEnum[],
  });

  return {
    ids: queryRes.ids[0] as string[],
    embeddings: (queryRes.embeddings?.[0] ?? []) as number[][],
    documents: queryRes?.documents[0] as unknown as string[],
    metadatas: queryRes.metadatas as (Record<
      string,
      string | number | boolean
    > | null)[][],
    distances: (queryRes.distances ?? []) as number[][],
  };
}

export async function deleteDocument(id: string): Promise<boolean> {
  if (!id) return false;
  if (typeof id !== "string") return false;

  try {
    const collection = await getKnowledgeCollection();
    await collection.delete({
      ids: [id],
    });
    return true;
  } catch (error) {
    console.error(error);
    return false;
  }
}

export async function returnAllDocuments(): Promise<
  {
    title: string;
    source: string;
    text: string;
  }[]
> {
  const collection = await getKnowledgeCollection();
  const data = await collection.peek({ limit: 100_000 });

  const entries = data.ids.map((id, index) => ({
    title: id,
    source: data.metadatas[index]?.link?.toString() || "",
    text: data.documents[index] || "",
  }));

  return entries;
}

export async function updateDocumentSource(
  id: string,
  newSource: string,
): Promise<boolean> {
  if (!id || !newSource) return false;
  if (typeof id !== "string" || typeof newSource !== "string") return false;

  try {
    const collection = await getKnowledgeCollection();

    // Get the existing document to preserve text and embeddings
    const existing = await collection.get({
      ids: [id],
      include: ["documents", "embeddings", "metadatas"] as IncludeEnum[],
    });

    if (!existing.ids.length) {
      console.error(`Document with ID ${id} not found`);
      return false;
    }

    // Update with existing embeddings and document, but new metadata
    // Handle potential null values from ChromaDB
    const documents =
      existing.documents?.filter((doc): doc is string => doc !== null) || [];
    const embeddings =
      existing.embeddings?.filter((emb): emb is number[] => emb !== null) || [];

    if (documents.length === 0) {
      console.error(`No valid document found for ID ${id}`);
      return false;
    }

    await collection.upsert({
      documents: documents,
      ids: [id],
      embeddings: embeddings.length > 0 ? embeddings : undefined,
      metadatas: [{ link: newSource, date: Date.now() }],
    });

    return true;
  } catch (error) {
    console.error(`Error updating document source for ${id}:`, error);
    return false;
  }
}

export default {
  UpsertDocument,
  similaritySearch,
  deleteDocument,
  returnAllDocuments,
  updateDocumentSource,
};
