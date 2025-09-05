import { Client } from "discord.js";
import { TrainOnDiscordThreads } from "./discord/index.js";
import { TrainOnGithubIssues } from "./issues/index.js";
import { StartTrainingService } from "./repos/index.js";
import cron from "node-cron";
import { env } from "../env.js";

export async function BeginTrainingProcesses(client: Client) {
  console.log("Starting Training Processes");

  cron.schedule("0 0 * * 0", async () => {
    console.log("Training on Discord Threads");
    await TrainOnDiscordThreads(client);
    console.log("Training on GitHub Issues");
    await TrainOnGithubIssues(client);
    console.log("Training on Repositories");
    await StartTrainingService();
  });
}
