import { ChatInputCommandInteraction, MessageFlags } from "discord.js";
import { CustomEmbed } from "../utils/embedBuilder.js";
import { env } from "../env.js";

interface MCPRequest {
  requestId: string;
  userId: string;
  username: string;
  query: string;
  timestamp: number;
}

interface TimeRangeStats {
  userRequests: number;
  totalRequests?: number;
  uniqueUsers?: number;
  avgRequestsPerUser?: number;
}

interface StatsResult {
  "24h": TimeRangeStats;
  "7d": TimeRangeStats;
  "30d": TimeRangeStats;
  lifetime: TimeRangeStats;
}

/**
 * Calculate stats for a specific time range
 */
function calculateStatsForRange(
  requests: MCPRequest[],
  userId: string,
  cutoffTime: number,
  includeGlobalStats: boolean,
): TimeRangeStats {
  const filteredRequests = requests.filter((r) => r.timestamp >= cutoffTime);

  const userRequests = filteredRequests.filter(
    (r) => r.userId === userId,
  ).length;

  if (!includeGlobalStats) {
    return { userRequests };
  }

  // Calculate global stats for admin
  const totalRequests = filteredRequests.length;
  const uniqueUsers = new Set(filteredRequests.map((r) => r.userId)).size;
  const avgRequestsPerUser =
    uniqueUsers > 0 ? Math.round((totalRequests / uniqueUsers) * 10) / 10 : 0;

  return {
    userRequests,
    totalRequests,
    uniqueUsers,
    avgRequestsPerUser,
  };
}

/**
 * Get all MCP requests from the database
 */
function getAllRequests(): MCPRequest[] {
  const requests: MCPRequest[] = [];

  try {
    for (const { value } of globalThis.databases.mcpRequests.getRange()) {
      if (value && typeof value === "object") {
        requests.push(value as MCPRequest);
      }
    }
  } catch (error) {
    console.error("Error reading MCP requests from database:", error);
  }

  return requests;
}

/**
 * Calculate comprehensive stats for a user
 */
function calculateStats(userId: string, isAdmin: boolean): StatsResult {
  const requests = getAllRequests();
  const now = Date.now();

  const ranges = {
    "24h": now - 24 * 60 * 60 * 1000,
    "7d": now - 7 * 24 * 60 * 60 * 1000,
    "30d": now - 30 * 24 * 60 * 60 * 1000,
    lifetime: 0,
  };

  return {
    "24h": calculateStatsForRange(requests, userId, ranges["24h"], isAdmin),
    "7d": calculateStatsForRange(requests, userId, ranges["7d"], isAdmin),
    "30d": calculateStatsForRange(requests, userId, ranges["30d"], isAdmin),
    lifetime: calculateStatsForRange(requests, userId, ranges.lifetime, isAdmin),
  };
}

/**
 * Format stats into a Discord embed
 */
function formatStatsEmbed(
  username: string,
  stats: StatsResult,
  isAdmin: boolean,
): CustomEmbed {
  const embed = new CustomEmbed()
    .setTitle("📊 MCP Usage Statistics")
    .setDescription(`Statistics for **${username}**`);

  // Personal stats section
  embed.addFields({
    name: "👤 Your Requests",
    value:
      `**Last 24 hours:** ${stats["24h"].userRequests}\n` +
      `**Last 7 days:** ${stats["7d"].userRequests}\n` +
      `**Last 30 days:** ${stats["30d"].userRequests}\n` +
      `**Lifetime:** ${stats.lifetime.userRequests}`,
    inline: false,
  });

  // Admin stats section
  if (isAdmin) {
    embed.addFields(
      {
        name: "🌍 Total Requests (All Users)",
        value:
          `**Last 24 hours:** ${stats["24h"].totalRequests}\n` +
          `**Last 7 days:** ${stats["7d"].totalRequests}\n` +
          `**Last 30 days:** ${stats["30d"].totalRequests}\n` +
          `**Lifetime:** ${stats.lifetime.totalRequests}`,
        inline: true,
      },
      {
        name: "👥 Unique Users",
        value:
          `**Last 24 hours:** ${stats["24h"].uniqueUsers}\n` +
          `**Last 7 days:** ${stats["7d"].uniqueUsers}\n` +
          `**Last 30 days:** ${stats["30d"].uniqueUsers}\n` +
          `**Lifetime:** ${stats.lifetime.uniqueUsers}`,
        inline: true,
      },
      {
        name: "📈 Avg Requests/User",
        value:
          `**Last 24 hours:** ${stats["24h"].avgRequestsPerUser}\n` +
          `**Last 7 days:** ${stats["7d"].avgRequestsPerUser}\n` +
          `**Last 30 days:** ${stats["30d"].avgRequestsPerUser}\n` +
          `**Lifetime:** ${stats.lifetime.avgRequestsPerUser}`,
        inline: true,
      },
    );
  }

  return embed;
}

/**
 * Handle the /mcp-stats command
 */
export async function handleMCPStatsCommand(
  interaction: ChatInputCommandInteraction,
) {
  try {
    const userId = interaction.user.id;
    const username = interaction.user.username;
    const isAdmin = env.ADMIN_USERS.includes(userId);

    // Calculate stats
    const stats = calculateStats(userId, isAdmin);

    // Format and send embed
    const embed = formatStatsEmbed(username, stats, isAdmin);

    await interaction.reply({
      embeds: [embed],
      flags: MessageFlags.Ephemeral,
    });

    console.log(`MCP stats sent to ${username} (${userId})`);
  } catch (error) {
    console.error("Error handling MCP stats command:", error);
    await interaction.reply({
      embeds: [
        new CustomEmbed()
          .setDescription(
            "❌ Sorry, I encountered an error while retrieving your MCP statistics. Please try again later.",
          )
          .setRed(),
      ],
      ephemeral: true,
    });
  }
}
