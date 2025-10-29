import { ChatInputCommandInteraction, MessageFlags } from "discord.js";
import { CustomEmbed } from "../utils/embedBuilder.js";
import { randomBytes } from "crypto";
import { env } from "../env.js";

function generateToken(): string {
  return randomBytes(32).toString("hex");
}

export async function handleMCPCommand(
  interaction: ChatInputCommandInteraction,
) {
  try {
    // Check if user already has a token
    const existingTokenEntry = Array.from(
      globalThis.databases.mcpTokens.getRange(),
    ).find(({ value }: any) => value.userId === interaction.user.id);

    let token: string;
    let isNewToken = false;

    if (existingTokenEntry) {
      // User already has a token
      token = existingTokenEntry.value.token;
    } else {
      // Generate new token
      token = generateToken();
      isNewToken = true;

      // Store token in database
      globalThis.databases.mcpTokens.put(token, {
        token,
        userId: interaction.user.id,
        username: interaction.user.username,
        createdAt: Date.now(),
      });

      console.log(
        `Generated new MCP token for user ${interaction.user.username} (${interaction.user.id})`,
      );
    }

    // Create setup guide embed
    const mcpUrl = `${env.MCP_SERVER_URL}/mcp`;

    const setupEmbed = new CustomEmbed()
      .setTitle("🔐 Aztec Knowledge MCP Server")
      .setDescription(
        isNewToken
          ? "Your unique authentication token has been generated! Add this MCP server to any MCP-compatible client."
          : "Here is your existing authentication token.",
      )
      .addFields(
        {
          name: "🔑 Your Token",
          value: `\`\`\`${token}\`\`\``,
          inline: false,
        },
        {
          name: "🔧 Setup for Claude Code",
          value:
            "Run one of these commands in your terminal:\n" +
            "```bash\n" +
            `# Local (current project only)\n` +
            `claude mcp add --transport http aztec-knowledge --scope local ${mcpUrl} --header "Authorization: Bearer ${token}"\n\n` +
            `# User (all your projects)\n` +
            `claude mcp add --transport http aztec-knowledge --scope user ${mcpUrl} --header "Authorization: Bearer ${token}"\n\n` +
            `# Project (shared via .mcp.json)\n` +
            `claude mcp add --transport http aztec-knowledge --scope project ${mcpUrl} --header "Authorization: Bearer ${token}"\n` +
            "```",
          inline: false,
        },
        {
          name: "🔧 Setup for Cursor",
          value:
            "1. Open Cursor settings (Cmd/Ctrl + ,)\n" +
            "2. Search for 'MCP' or 'Model Context Protocol'\n" +
            "3. Add this configuration:\n" +
            "```json\n" +
            `{\n  "url": "${mcpUrl}",\n  "headers": {\n    "Authorization": "Bearer ${token}"\n  }\n}\n` +
            "```",
          inline: false,
        },
        {
          name: "📖 Available Tool",
          value:
            "**query_knowledge** - Search Aztec documentation\n" +
            "Ask questions like:\n" +
            "• How do I create a private token?\n" +
            "• Explain the Aztec protocol",
          inline: false,
        },
        {
          name: "⚡ Rate Limits",
          value: env.MCP_RATE_LIMIT_ENABLED
            ? `${env.MCP_RATE_LIMIT_PER_MINUTE} requests/min`
            : "Unlimited",
          inline: true,
        },
        {
          name: "🔒 Security",
          value: "Keep your token private!",
          inline: true,
        },
      );

    await interaction.reply({
      embeds: [setupEmbed],
      flags: MessageFlags.Ephemeral,
    });

    console.log(
      `MCP setup guide sent to ${interaction.user.username} (${interaction.user.id})`,
    );
  } catch (error) {
    console.error("Error handling MCP command:", error);
    await interaction.reply({
      embeds: [
        new CustomEmbed()
          .setDescription(
            "❌ Sorry, I encountered an error while generating your MCP token. Please try again later.",
          )
          .setRed(),
      ],
      ephemeral: true,
    });
  }
}
