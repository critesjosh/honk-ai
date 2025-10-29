import { FastifyRequest, FastifyReply } from "fastify";
import { env } from "../env.js";

interface RateLimitData {
  count: number;
  resetTime: number;
}

// Store rate limit data per token
const rateLimitStore = new Map<string, RateLimitData>();

// Cleanup old entries every minute
setInterval(() => {
  const now = Date.now();
  for (const [token, data] of rateLimitStore.entries()) {
    if (now > data.resetTime) {
      rateLimitStore.delete(token);
    }
  }
}, 60000);

/**
 * Rate limiting middleware for MCP server
 * Limits requests per token per minute
 * Can be disabled via MCP_RATE_LIMIT_ENABLED env variable
 */
export async function rateLimitMiddleware(
  request: FastifyRequest,
  reply: FastifyReply,
) {
  // Skip rate limiting if disabled or auth is disabled (no token to track)
  if (!env.MCP_RATE_LIMIT_ENABLED || !env.MCP_AUTH_REQUIRED) {
    return;
  }

  const token = (request as any).mcpToken;

  // If no token (shouldn't happen after auth middleware), skip
  if (!token) {
    return;
  }

  const now = Date.now();
  const resetWindow = 60000; // 1 minute

  // Get or create rate limit data for this token
  let rateLimitData = rateLimitStore.get(token);

  if (!rateLimitData || now > rateLimitData.resetTime) {
    // Create new window
    rateLimitData = {
      count: 0,
      resetTime: now + resetWindow,
    };
    rateLimitStore.set(token, rateLimitData);
  }

  // Increment request count
  rateLimitData.count++;

  // Check if limit exceeded
  if (rateLimitData.count > env.MCP_RATE_LIMIT_PER_MINUTE) {
    const resetIn = Math.ceil((rateLimitData.resetTime - now) / 1000);

    return reply.code(429).send({
      error: "Rate limit exceeded",
      message: `Too many requests. Please try again in ${resetIn} seconds.`,
      limit: env.MCP_RATE_LIMIT_PER_MINUTE,
      resetIn,
    });
  }

  // Add rate limit headers
  reply.header("X-RateLimit-Limit", env.MCP_RATE_LIMIT_PER_MINUTE);
  reply.header(
    "X-RateLimit-Remaining",
    Math.max(0, env.MCP_RATE_LIMIT_PER_MINUTE - rateLimitData.count),
  );
  reply.header("X-RateLimit-Reset", Math.ceil(rateLimitData.resetTime / 1000));

  console.log(
    `Rate limit: ${rateLimitData.count}/${env.MCP_RATE_LIMIT_PER_MINUTE} for token ${token.substring(0, 8)}...`,
  );
}
