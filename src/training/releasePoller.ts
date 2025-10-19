import { Client, TextChannel } from "discord.js";
import { getLatestAztecRelease } from "../utils/github.js";
import { env } from "../env.js";
import { TrainOnDiscordThreads } from "./discord/index.js";
import { TrainOnGithubIssues } from "./issues/index.js";
import { StartTrainingService } from "./repos/index.js";

/**
 * Starts the release polling service
 * Checks for new aztec-packages releases every hour and triggers re-indexing when detected
 */
export function startReleasePoller(client: Client) {
  console.log("Starting release polling service (checking every hour)");

  // Check immediately on startup
  checkForNewRelease(client);

  // Check every hour (3600000ms)
  setInterval(
    () => {
      checkForNewRelease(client);
    },
    60 * 60 * 1000,
  );
}

/**
 * Checks for a new release and triggers training if one is found
 */
async function checkForNewRelease(client: Client) {
  try {
    console.log("Checking for new aztec-packages release...");
    const latestRelease = await getLatestAztecRelease();
    const lastProcessedRelease = globalThis.databases.releaseTracking.get(
      "lastProcessedRelease",
    );

    // If this is a new release (and not the first run)
    if (lastProcessedRelease && latestRelease !== lastProcessedRelease) {
      console.log(
        `New release detected: ${latestRelease} (previous: ${lastProcessedRelease})`,
      );

      // Log to Discord
      await logToDiscord(
        client,
        `🚀 New aztec-packages release detected: ${latestRelease}. Starting re-indexing...`,
      );

      // Temporarily override AZTEC_DOCS_VERSION for this training run
      const originalVersion = env.AZTEC_DOCS_VERSION;
      (env as any).AZTEC_DOCS_VERSION = latestRelease;

      try {
        // Run full training pipeline
        console.log("Running full training pipeline for new release...");
        await StartTrainingService();
        await TrainOnDiscordThreads(client);
        await TrainOnGithubIssues(client);

        // Update the last processed release
        globalThis.databases.releaseTracking.put(
          "lastProcessedRelease",
          latestRelease,
        );

        await logToDiscord(
          client,
          `✅ Successfully re-indexed for release: ${latestRelease}`,
        );
        console.log(`Successfully processed new release: ${latestRelease}`);
      } catch (error) {
        await logToDiscord(
          client,
          `❌ Error during re-indexing for ${latestRelease}: ${error instanceof Error ? error.message : "Unknown error"}`,
        );
        console.error("Error during release re-indexing:", error);
      } finally {
        // Restore original AZTEC_DOCS_VERSION
        (env as any).AZTEC_DOCS_VERSION = originalVersion;
      }
    } else if (!lastProcessedRelease) {
      // First run - trigger initial training
      console.log(
        `No previous release found in database. Triggering initial training for: ${latestRelease}`,
      );

      await logToDiscord(
        client,
        `🚀 Starting initial training for version: ${latestRelease}`,
      );

      // Temporarily override AZTEC_DOCS_VERSION for this training run
      const originalVersion = env.AZTEC_DOCS_VERSION;
      (env as any).AZTEC_DOCS_VERSION = latestRelease;

      try {
        console.log(`Starting initial training for version: ${latestRelease}`);
        await StartTrainingService();
        await TrainOnDiscordThreads(client);
        await TrainOnGithubIssues(client);

        // Update the last processed release
        globalThis.databases.releaseTracking.put(
          "lastProcessedRelease",
          latestRelease,
        );

        await logToDiscord(
          client,
          `✅ Initial training completed for release: ${latestRelease}`,
        );
        console.log(
          `Successfully completed initial training: ${latestRelease}`,
        );
      } catch (error) {
        await logToDiscord(
          client,
          `❌ Error during initial training for ${latestRelease}: ${error instanceof Error ? error.message : "Unknown error"}`,
        );
        console.error("Error during initial training:", error);
      } finally {
        // Restore original AZTEC_DOCS_VERSION
        (env as any).AZTEC_DOCS_VERSION = originalVersion;
      }
    } else {
      console.log("No new release detected");
    }
  } catch (error) {
    console.error("Error in release poller:", error);
  }
}

/**
 * Logs a message to the Discord logging channel
 */
async function logToDiscord(client: Client, message: string) {
  try {
    const loggingChannel = (await client.channels.fetch(
      env.LoggingChannelId,
    )) as TextChannel;
    if (loggingChannel) {
      await loggingChannel.send(message);
    }
  } catch (error) {
    console.error("Error logging to Discord:", error);
  }
}
