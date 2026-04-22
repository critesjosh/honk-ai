import type { DocsGPTClient } from "./client.js";
import { DocsGPTClientError } from "./client.js";

export function createToolHandlers(client: DocsGPTClient) {
  async function searchAztec(args: { query: string; chunks?: number }) {
    try {
      const results = await client.search(args.query, args.chunks ?? 5);

      if (results.length === 0) {
        return {
          content: [
            {
              type: "text" as const,
              text: "No results found for this query.",
            },
          ],
        };
      }

      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify({ results }, null, 2),
          },
        ],
      };
    } catch (err) {
      const message =
        err instanceof DocsGPTClientError
          ? err.message
          : `Unexpected error: ${err instanceof Error ? err.message : String(err)}`;

      return {
        content: [{ type: "text" as const, text: `Error: ${message}` }],
        isError: true as const,
      };
    }
  }

  return { searchAztec };
}
