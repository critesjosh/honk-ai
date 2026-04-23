#!/usr/bin/env node

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { loadConfig } from "./config.js";
import { DocsGPTClient } from "./client.js";
import { createToolHandlers } from "./tools.js";

const config = loadConfig();
const client = new DocsGPTClient(config);
const { searchAztec } = createToolHandlers(client);

const server = new McpServer({
  name: "docsgpt-aztec",
  version: "0.1.0",
});

// Tool: search_aztec — raw vector search, no LLM synthesis
server.tool(
  "search_aztec",
  "Search the Aztec Network knowledge base for relevant document chunks. " +
    "Returns raw text excerpts from developer docs, Aztec.nr framework, " +
    "aztec.js SDK, example contracts, protocol circuits, and more. " +
    "The knowledge base is pinned to Aztec v4.2.0.",
  {
    query: z.string().describe("Search query for Aztec documentation and source code"),
    chunks: z
      .number()
      .optional()
      .describe("Number of results to return (default: 5)"),
  },
  async (args) => searchAztec(args)
);

// Resource: aztec://knowledge-base — static corpus metadata
server.resource(
  "knowledge-base",
  "aztec://knowledge-base",
  {
    description: "Aztec Network knowledge base metadata and usage guidance",
    mimeType: "text/plain",
  },
  async () => ({
    contents: [
      {
        uri: "aztec://knowledge-base",
        text:
          "Aztec Network Knowledge Base (v4.2.0)\n\n" +
          "Sources indexed:\n" +
          "- Developer Docs: 96 versioned .md/.mdx files\n" +
          "- Operator Docs: 39 setup/operations docs\n" +
          "- Aztec.nr Framework: 223 Noir contract library files\n" +
          "- Example Contracts: 207 reference Noir contracts\n" +
          "- Protocol Circuits: 454 Noir circuit logic files\n" +
          "- aztec.js SDK: 73 TypeScript files\n" +
          "- Aztec CLI + Wallet CLI: 93 TypeScript files\n" +
          "- E2E Tests: 245 TypeScript integration tests\n" +
          "- L1 Contracts: 347 Solidity files\n\n" +
          "Source-code files (.nr/.ts/.sol) are indexed with a .txt suffix so the\n" +
          "DocsGPT ingest recognizes them; the original filename + path is\n" +
          "preserved in each chunk's metadata. Pinned to aztec-packages tag\n" +
          "v4.2.0.\n\n" +
          "Use search_aztec to retrieve raw documentation and code excerpts.\n",
      },
    ],
  })
);

// Prompt: aztec_quickstart — getting started template
server.prompt(
  "aztec_quickstart",
  "Get started exploring Aztec Network development topics",
  {
    topic: z
      .string()
      .optional()
      .describe(
        "The Aztec topic to explore (e.g., 'private state', 'contracts', 'aztec.js')"
      ),
  },
  async (args) => ({
    messages: [
      {
        role: "user" as const,
        content: {
          type: "text" as const,
          text:
            `I want to learn about ${args.topic || "private state"} in Aztec Network. ` +
            "Please use search_aztec to find relevant documentation and code, " +
            "then explain the key concepts with examples from the knowledge base.",
        },
      },
    ],
  })
);

// Start the server with stdio transport
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error("Fatal error:", err instanceof Error ? err.message : err);
  process.exit(1);
});
