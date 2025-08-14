import { z } from "zod";
import dotenv from "dotenv";

dotenv.config();

const envSchema = z.object({
  BOT_TOKEN: z.string(),
  BOT_ID: z.string(),

  AllowedChannelIds: z.string().transform((val) => val.split(",")),
  LoggingChannelId: z.string(),
  AnalyticsChannelId: z.string(),
  MessageHistoryPerThread: z.string().transform((val) => parseInt(val, 10)),
  DiscordTrainingForums: z.string().transform((val) => val.split(",")),
  RequestsPerMinute: z.string().transform((val) => parseInt(val, 10)),

  CHROMADB_URL_PATH: z.string(),
  CHROMADB_URL_PORT: z.string().transform((val) => parseInt(val, 10)),
  CHROMADB_URL_SSL: z.string().transform((val) => val === "true"),
  CHROMADB_KNOWLEDGEBASE_ID: z.string(),
  CHROMADB_AUTH: z.string(),

  OPEN_AI_API: z.string(),
  EMBEDDING_MODEL: z.string(),
  AMOUNT_OF_DOCS: z.string().transform((val) => parseInt(val, 10)),

  // LLM provider selection and model configuration
  LLM_PROVIDER: z.enum(["openai", "anthropic"]).default("openai"),
  OPENAI_MODEL: z.string(),

  ANTHROPIC_API: z.string(),
  ANTHROPIC_MODEL: z.string(),
  ANTHROPIC_THINKING: z.string().transform((val) => val === "true"),
  MAX_RESPONSE_TOKENS: z.string().transform((val) => parseInt(val, 10)),

  // Docs/testnet tag used in markdown processing
  DOCS_TESTNET_TAG: z.string().default("1.2.0"),

  GITHUB_TOKEN: z.string(),
  LMDB_ROUTE: z.string(),
  SNAPSHOT_ROUTE: z.string(),
});

export const env = envSchema.parse(process.env);
export type Env = z.infer<typeof envSchema>;
