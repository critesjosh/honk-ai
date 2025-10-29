import { Client, TextChannel } from "discord.js";
import { Octokit } from "@octokit/rest";
import { env } from "../../env.js";
import llm from "../../utils/llm.js";
import { UpsertDocument } from "../../utils/chroma.js";
import issuesConfig from "../../config/issues.json" with { type: "json" };
import prompts from "../../config/prompts.js";

const octokit = new Octokit({
  auth: env.GITHUB_TOKEN,
});

export async function TrainOnGithubIssues(client: Client) {
  for (const repo of issuesConfig.GITHUB_REPO_ISSUES) {
    // Create an array to store the issues that Honk has already been trained on
    let trainedIssues: number[] = [];

    // Fetch the list of issues that Honk has already been trained on from the database
    try {
      const result = await globalThis.databases.trainedIssues.get(repo);
      trainedIssues = Array.isArray(result) ? result : [];
    } catch (error) {
      console.log("Error fetching issues from LMDB, likely empty:", error);
    }

    // Track how many issues we've processed
    let processedCount = 0;

    console.log(`Processing up to ${env.TRAINING_LIMIT} most recent issues for ${repo}`);

    for await (const issue of getAllIssues(
      issuesConfig.GITHUB_REPO_OWNER,
      repo,
    )) {
      try {
        // Check the database to see if it already includes this issue
        if (trainedIssues.includes(issue.number)) {
          console.log("Reached already trained, breaking");
          break;
        }

        // Always limit to most recent issues (TRAINING_LIMIT)
        if (processedCount >= env.TRAINING_LIMIT) {
          console.log(`Reached training limit of ${env.TRAINING_LIMIT} issues`);
          break;
        }

        console.log(`Processing issue #${issue.number}: ${issue.title}`);

        // Most bot messages are automated and useless, so we can skip them (Especially issue's for bumping and what not)
        if (issue.user == null || issue.user.login.includes("[bot]")) {
          console.log("Skipping issue, maybe dependabot or an automated task.");
          processedCount++; // Still count skipped issues towards limit
          continue;
        }

        // An array to store all the comments of the current issue in the loop
        const allComments: string[] = [];
        for await (const comment of getCommentsForIssue(
          issuesConfig.GITHUB_REPO_OWNER,
          repo,
          issue.number,
        )) {
          // Ignore all comments by bots
          if (comment.user == null || comment.user.login.includes("[bot]")) {
            continue;
          }
          allComments.push(
            `${comment.user.login}: ${comment.body} (${comment.html_url})`,
          );
        }

        // Full context of the issue to pass into the LLM for summarization
        const completedData = `Github Issue Title: ${
          issue.title
        } Github Issue Number: ${issue.number} Github Issue URL: ${
          issue.html_url
        }
            
            Issue Body: ${issue.body}
            
            Issue Comments:
            ${allComments.join("\n")}`;

        // Generate response using the anthropic utility with prompt from config
        const prompt = prompts.getGithubIssuesPrompt(
          completedData,
          issue.title,
          issue.number,
        );
        const questionResponse = await llm.generateResponse(
          prompt.system,
          prompt.human,
        );

        // Format the training data
        const TrainingData = `#${issue.number}: ${
          issue.title
        }\nAI Summary:\n${questionResponse}\nAll Conversation\n${allComments.join(
          "\n",
        )}`;

        // Upsert the document into ChromaDB and push to the Array
        await UpsertDocument(issue.html_url, TrainingData, issue.html_url);
        trainedIssues.push(issue.number);

        // Add the issue number to the list of trained issues
        // We do the training here so incase of a crash during training we dont lose the progress in a sense
        console.log(
          await globalThis.databases.trainedIssues.put(repo, trainedIssues),
        );

        console.log("Trained on:", issue.html_url);
        processedCount++;

        try {
          // await (client.channels.cache.get(env.LoggingChannelId) as TextChannel).send({
          //   content: `trained on ${issue.html_url}`,
          // });
        } catch (e) {
          console.error("Error notifying training:", e);
        }
      } catch (e) {
        console.error("An Error Occurred:", e);
      }
    }
  }
}

export async function* getAllIssues(owner: string, repo: string) {
  let page = 1;
  while (true) {
    const response = await octokit.rest.issues.listForRepo({
      owner,
      repo,
      state: "all",
      per_page: 100,
      page: page++,
    });
    if (response.data.length === 0) break;
    for (const issue of response.data) {
      yield issue;
    }
  }
}

export async function* getCommentsForIssue(
  owner: string,
  repo: string,
  issue_number: number,
) {
  let page = 1;
  while (true) {
    const response = await octokit.rest.issues.listComments({
      owner,
      repo,
      issue_number,
      per_page: 100,
      page: page++,
    });
    if (response.data.length === 0) break;
    for (const comment of response.data) {
      yield comment;
    }
  }
}
