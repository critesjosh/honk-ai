import { Client } from "discord.js";

import { open } from "lmdb";
import * as path from "node:path";
import cron from "node-cron";
import { env } from "../env.js";

import { BeginTrainingProcesses } from "../training/index.js";

import { newMessage } from "./messages.js";
import { handleButtonInteraction, handleModalSubmit } from "./interactions.js";

export default async (client: Client) => {
  // Create the databases
  globalThis.databases = {
    // Used to store the thumbsup/down votes on a response
    analytics: open({
      path: path.join(env.LMDB_ROUTE, "analytics"),
      cache: true,
    }),

    // All the documents that are stored from ChromaDB
    documents: open({
      path: path.join(env.LMDB_ROUTE, "documents"),
      cache: true,
    }),

    // All the responses to questions from the AI
    responses: open({
      path: path.join(env.LMDB_ROUTE, "responses"),
      cache: true,
    }),

    // User questions for analytics
    questions: open({
      path: path.join(env.LMDB_ROUTE, "questions"),
      cache: true,
    }),

    // All Discord threads that the AI has trained on
    trainedThreads: open({
      path: path.join(env.LMDB_ROUTE, "trainedThreads"),
      cache: true,
    }),

    // All github issues that the AI has trained on
    trainedIssues: open({
      path: path.join(env.LMDB_ROUTE, "trainedIssues"),
      cache: true,
    }),
  };

  // Initialize analytics counters if they don't exist
  if (!globalThis.databases.analytics.get("totalQuestions")) {
    globalThis.databases.analytics.put("totalQuestions", "0");
  }
  if (!globalThis.databases.analytics.get("totalGoodRatings")) {
    globalThis.databases.analytics.put("totalGoodRatings", "0");
  }
  if (!globalThis.databases.analytics.get("totalBadRatings")) {
    globalThis.databases.analytics.put("totalBadRatings", "0");
  }
  if (!globalThis.databases.analytics.get("totalRatings")) {
    globalThis.databases.analytics.put("totalRatings", "0");
  }

  console.log("Analytics initialized");
  console.log("Starting Message Handler");

  // Handle users querying the AI
  client.on("messageCreate", async (message) => {
    await newMessage(client, message);
  });

  console.log("Starting Interaction Handler");

  // Listen for Interaction's (buttons, modals, etc)
  client.on("interactionCreate", async (interaction) => {
    if (interaction.isButton()) {
      await handleButtonInteraction(interaction);
    } else if (interaction.isModalSubmit()) {
      await handleModalSubmit(interaction);
    }
  });

  client.on("ready", async () => {
    console.log("Beginning training processes...");
    await BeginTrainingProcesses(client);
  });
};
