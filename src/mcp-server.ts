import { generateEmbedding } from "./utils/embeddings.js";
import { similaritySearch } from "./utils/chroma.js";
import { env } from "./env.js";
import Fastify from "fastify";
import cors from "@fastify/cors";
import { randomBytes } from "crypto";

const PORT = env.MCP_PORT;

// Session storage
interface Session {
  id: string;
  token?: string;
  userId?: string;
  username?: string;
  createdAt: number;
  lastActivity: number;
  requestCount: number;
  requestResetTime: number;
}

const sessions = new Map<string, Session>();

// Cleanup old sessions every 5 minutes
setInterval(
  () => {
    const now = Date.now();
    const fiveMinutes = 5 * 60 * 1000;

    for (const [sessionId, session] of sessions.entries()) {
      if (now - session.lastActivity > fiveMinutes) {
        sessions.delete(sessionId);
        console.log(`Cleaned up inactive session: ${sessionId}`);
      }
    }
  },
  5 * 60 * 1000,
);

/**
 * Authenticate request and manage session
 */
function authenticateRequest(
  authHeader: string | undefined,
  sessionId: string | undefined,
): { session: Session; error?: string } {
  // If auth is disabled, create anonymous session
  if (!env.MCP_AUTH_REQUIRED) {
    const anonSessionId = sessionId || randomBytes(16).toString("hex");
    let session = sessions.get(anonSessionId);

    if (!session) {
      session = {
        id: anonSessionId,
        createdAt: Date.now(),
        lastActivity: Date.now(),
        requestCount: 0,
        requestResetTime: Date.now() + 60000,
      };
      sessions.set(anonSessionId, session);
    }

    return { session };
  }

  // Extract token from Authorization header
  if (!authHeader) {
    return {
      session: null as any,
      error:
        "Missing Authorization header. Please provide: Authorization: Bearer <token>",
    };
  }

  const match = authHeader.match(/^Bearer\s+(\S+)$/);
  if (!match) {
    return {
      session: null as any,
      error: 'Invalid Authorization format. Use: "Bearer <token>"',
    };
  }

  const token = match[1];

  // Validate token
  try {
    const tokenData = globalThis.databases.mcpTokens.get(token);

    if (!tokenData) {
      return {
        session: null as any,
        error: "Invalid token. Use /mcp command in Discord to get a token.",
      };
    }

    // Get or create session
    const newSessionId = sessionId || randomBytes(16).toString("hex");
    let session = sessions.get(newSessionId);

    if (!session) {
      session = {
        id: newSessionId,
        token,
        userId: tokenData.userId,
        username: tokenData.username,
        createdAt: Date.now(),
        lastActivity: Date.now(),
        requestCount: 0,
        requestResetTime: Date.now() + 60000,
      };
      sessions.set(newSessionId, session);
      console.log(`New session created for user: ${tokenData.username}`);
    } else {
      session.lastActivity = Date.now();
    }

    return { session };
  } catch (error) {
    console.error("Error validating token:", error);
    return {
      session: null as any,
      error: "Failed to validate token",
    };
  }
}

/**
 * Check rate limit for session
 */
function checkRateLimit(session: Session): {
  allowed: boolean;
  error?: string;
} {
  if (!env.MCP_RATE_LIMIT_ENABLED || !env.MCP_AUTH_REQUIRED) {
    return { allowed: true };
  }

  const now = Date.now();

  // Reset counter if window expired
  if (now > session.requestResetTime) {
    session.requestCount = 0;
    session.requestResetTime = now + 60000;
  }

  // Check limit
  session.requestCount++;

  if (session.requestCount > env.MCP_RATE_LIMIT_PER_MINUTE) {
    const resetIn = Math.ceil((session.requestResetTime - now) / 1000);
    return {
      allowed: false,
      error: `Rate limit exceeded. Please try again in ${resetIn} seconds. (Limit: ${env.MCP_RATE_LIMIT_PER_MINUTE}/min)`,
    };
  }

  return { allowed: true };
}

/**
 * Start the MCP HTTP server
 */
export async function startMCPServer() {
  const app = Fastify({
    logger: true,
  });

  // Enable CORS
  await app.register(cors, {
    origin: true,
    credentials: true,
  });

  // Health check endpoint
  app.get("/health", async () => {
    return { status: "ok", service: "honk-knowledge-mcp-server" };
  });

  // SSE endpoint storage (for GET /mcp)
  const sseConnections = new Map<string, any>();

  // MCP Protocol: GET endpoint for SSE streaming (server-to-client messages)
  app.get("/mcp", async (request, reply) => {
    const authHeader = request.headers.authorization as string | undefined;
    const existingSessionId = request.headers["mcp-session-id"] as
      | string
      | undefined;

    // Authenticate
    const { session, error } = authenticateRequest(
      authHeader,
      existingSessionId,
    );
    if (error) {
      return reply.code(401).send({ error });
    }

    // Set up SSE
    reply.raw.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
      "Mcp-Session-Id": session.id,
    });

    // Store connection
    sseConnections.set(session.id, reply.raw);

    // Send initial connect message
    reply.raw.write(
      `data: ${JSON.stringify({ type: "connected", sessionId: session.id })}\n\n`,
    );

    // Handle client disconnect
    request.raw.on("close", () => {
      sseConnections.delete(session.id);
      console.log(`SSE connection closed for session: ${session.id}`);
    });
  });

  // MCP Protocol: POST endpoint for JSON-RPC messages (client-to-server)
  app.post("/mcp", async (request, reply) => {
    const authHeader = request.headers.authorization as string | undefined;
    const existingSessionId = request.headers["mcp-session-id"] as
      | string
      | undefined;

    // Authenticate
    const { session, error: authError } = authenticateRequest(
      authHeader,
      existingSessionId,
    );
    if (authError) {
      return reply.code(401).send({
        jsonrpc: "2.0",
        error: { code: -32001, message: authError },
        id: null,
      });
    }

    // Check rate limit
    const { allowed, error: rateLimitError } = checkRateLimit(session);
    if (!allowed) {
      return reply.code(429).send({
        jsonrpc: "2.0",
        error: { code: -32002, message: rateLimitError },
        id: null,
      });
    }

    // Set session ID header
    reply.header("Mcp-Session-Id", session.id);

    try {
      const jsonRpcMessage = request.body as any;
      const { method, params, id } = jsonRpcMessage;

      // Handle initialize
      if (method === "initialize") {
        return reply.send({
          jsonrpc: "2.0",
          id,
          result: {
            protocolVersion: "2024-11-05",
            capabilities: {
              tools: {},
            },
            serverInfo: {
              name: "honk-knowledge-server",
              version: "1.0.0",
            },
          },
        });
      }

      // Handle tools/list
      if (method === "tools/list") {
        return reply.send({
          jsonrpc: "2.0",
          id,
          result: {
            tools: [
              {
                name: "query_knowledge",
                description:
                  "Search the Aztec knowledge base using semantic search. Returns relevant documentation chunks, code examples, and guides with source links.",
                inputSchema: {
                  type: "object",
                  properties: {
                    query: {
                      type: "string",
                      description:
                        "The search query - natural language question, topic, keyword, or concept",
                    },
                    limit: {
                      type: "number",
                      description:
                        "Maximum number of results (default: 4, max: 10)",
                      minimum: 1,
                      maximum: 10,
                      default: 4,
                    },
                  },
                  required: ["query"],
                },
              },
            ],
          },
        });
      }

      // Handle tools/call
      if (method === "tools/call") {
        const { name, arguments: args } = params;

        if (name === "query_knowledge") {
          const query = String(args.query);
          const limit = Math.min(Number(args.limit) || 4, 10);

          console.log(
            `Query from ${session.username || "anonymous"}: "${query}"`,
          );

          // Track request in database for analytics
          const requestId = `${Date.now()}-${randomBytes(8).toString("hex")}`;
          globalThis.databases?.mcpRequests?.put(requestId, {
            requestId,
            userId: session.userId || "anonymous",
            username: session.username || "anonymous",
            query,
            timestamp: Date.now(),
          });

          // Generate embedding
          const embedding = await generateEmbedding(query);

          // Search knowledge base
          const results = await similaritySearch({
            inputEmbedding: embedding,
            params: { limit },
          });

          // Format results
          const formattedResults = results.documents.map((doc, idx) => {
            const metadata = results.metadatas[0]?.[idx];
            const distance = results.distances?.[0]?.[idx];
            const relevanceScore = distance
              ? ((1 - distance) * 100).toFixed(1)
              : "N/A";

            return {
              content: doc,
              source: metadata?.link || "Unknown",
              relevance_percent: relevanceScore,
            };
          });

          return reply.send({
            jsonrpc: "2.0",
            id,
            result: {
              content: [
                {
                  type: "text",
                  text: JSON.stringify(
                    {
                      query,
                      results_count: formattedResults.length,
                      results: formattedResults,
                    },
                    null,
                    2,
                  ),
                },
              ],
            },
          });
        }

        return reply.send({
          jsonrpc: "2.0",
          id,
          error: {
            code: -32601,
            message: `Unknown tool: ${name}`,
          },
        });
      }

      // Handle ping
      if (method === "ping") {
        return reply.send({
          jsonrpc: "2.0",
          id,
          result: {},
        });
      }

      // Unknown method
      return reply.send({
        jsonrpc: "2.0",
        id,
        error: {
          code: -32601,
          message: `Method not found: ${method}`,
        },
      });
    } catch (error) {
      console.error("Error processing MCP request:", error);
      return reply.code(500).send({
        jsonrpc: "2.0",
        error: {
          code: -32603,
          message: error instanceof Error ? error.message : "Internal error",
        },
        id: null,
      });
    }
  });

  // Start server
  try {
    await app.listen({ port: PORT, host: "0.0.0.0" });
    console.log(
      `\n🚀 Aztec Knowledge MCP Server running on http://localhost:${PORT}`,
    );
    console.log(`MCP Endpoint: http://localhost:${PORT}/mcp`);
    console.log(`Health Check: http://localhost:${PORT}/health`);
    console.log(
      `\nAuthentication: ${env.MCP_AUTH_REQUIRED ? "ENABLED" : "DISABLED"}`,
    );
    console.log(
      `Rate Limiting: ${env.MCP_RATE_LIMIT_ENABLED ? `ENABLED (${env.MCP_RATE_LIMIT_PER_MINUTE}/min)` : "DISABLED"}`,
    );
    console.log(
      `\n${env.MCP_AUTH_REQUIRED ? "Use /mcp command in Discord to get your authentication token" : "No authentication required - server is open to all"}\n`,
    );
  } catch (err) {
    app.log.error(err);
    process.exit(1);
  }
}
