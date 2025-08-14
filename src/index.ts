import dotenv from "dotenv";

dotenv.config();

import { Client, GatewayIntentBits, Partials } from "discord.js";
import { env } from "./env.js";

import handler from "./handlers/index.js";
console.log(env);
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
    GatewayIntentBits.GuildMembers,
  ],
  partials: [
    Partials.Message,
    Partials.Channel,
    Partials.User,
    Partials.ThreadMember,
    Partials.GuildMember,
  ],
});

// All logic is managed under the handler for modularity
await handler(client);

client.login(env.BOT_TOKEN);
