import { FastifyRequest, FastifyReply } from "fastify";
import { env } from "../env.js";

/**
 * Authentication middleware for MCP server
 * Validates Bearer tokens against the mcpTokens database
 * Can be disabled via MCP_AUTH_REQUIRED env variable
 */
export async function authMiddleware(
  request: FastifyRequest,
  reply: FastifyReply,
) {
  // Skip auth if disabled
  if (!env.MCP_AUTH_REQUIRED) {
    return;
  }

  // Extract Authorization header
  const authHeader = request.headers.authorization;

  if (!authHeader) {
    return reply.code(401).send({
      error: "Missing Authorization header",
      message: "Please provide a Bearer token in the Authorization header",
    });
  }

  // Verify Bearer token format
  const match = authHeader.match(/^Bearer\s+(\S+)$/);
  if (!match) {
    return reply.code(401).send({
      error: "Invalid Authorization format",
      message: 'Authorization header must be in format: "Bearer <token>"',
    });
  }

  const token = match[1];

  // Validate token against database
  try {
    const tokenData = globalThis.databases.mcpTokens.get(token);

    if (!tokenData) {
      return reply.code(401).send({
        error: "Invalid token",
        message: "The provided token is not valid. Use /mcp command in Discord to get a token.",
      });
    }

    // Attach token data to request for use in rate limiting
    (request as any).mcpToken = token;
    (request as any).mcpUser = {
      userId: tokenData.userId,
      username: tokenData.username,
    };

    console.log(`Authenticated request from user: ${tokenData.username}`);
  } catch (error) {
    console.error("Error validating token:", error);
    return reply.code(500).send({
      error: "Authentication error",
      message: "Failed to validate token",
    });
  }
}
