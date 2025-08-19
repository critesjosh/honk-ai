import {
  Client,
  ChannelType,
  ThreadChannel,
  Channel,
  EmbedBuilder,
  TextChannel,
} from "discord.js";
import { env } from "../../env.js";
import prompts from "../../config/prompts.js";
import llm from "../../utils/llm.js";
import { UpsertDocument } from "../../utils/chroma.js";
import { CustomEmbed } from "../../utils/embedBuilder.js";

export async function TrainOnDiscordThreads(client: Client) {
  try {
    const forums = env.DiscordTrainingForums;

    for (const forum of forums) {
      // Fetch all the posts inside of the forum that are already in the database
      let alreadyTrainedThreads: string[] = [];
      try {
        const result = await globalThis.databases.trainedThreads.get(forum);
        alreadyTrainedThreads = Array.isArray(result) ? result : [];
      } catch (error) {
        console.error(`Error fetching ${forum}, likely empty:`, error);
        alreadyTrainedThreads = [];
      }

      // Fetch the forum channel data
      const forumChannel = await client.channels.fetch(forum);

      // Ensure the channel is a forum channel
      if (!forumChannel || forumChannel.type !== ChannelType.GuildForum) {
        console.error(`${forumChannel!.id} is not a forum channel`);
        continue;
      }

      // Fetch all active threads, and archived threads (Threads that are inactive for x amount of days become archived)
      const activeThreads = await forumChannel!.threads.fetch();
      const archivedThreads = await forumChannel!.threads.fetchArchived();

      // Initialize empty collections in case fetch results are undefined
      const activeThreadCollection = activeThreads?.threads || new Map();
      const archivedThreadCollection = archivedThreads?.threads || new Map();

      const allThreads = new Map([
        ...activeThreadCollection,
        ...archivedThreadCollection,
      ]);

      if (allThreads.size === 0) {
        console.error("Empty Forum");
        return false;
      }

      const threadIds: string[] = Array.from(allThreads.values()).map(
        (thread) => thread.id,
      );

      // Add the already trained threads into a new array to store the newely trained threads
      const newThreads: string[] = [...alreadyTrainedThreads];

      // Check if this is first startup (empty database) and limit threads
      const isFirstStartup = alreadyTrainedThreads.length === 0;
      const threadsToProcess = isFirstStartup 
        ? threadIds.slice(0, env.FIRST_STARTUP_LIMIT)
        : threadIds;

      for (const threadId of threadsToProcess) {
        if (newThreads.includes(threadId)) {
          console.log("Stopping, reach already trained!");
          break;
        }

        // Fetch the channel from the threadId
        const channel: Channel | null = await client.channels.fetch(threadId);

        // Shouldnt occur, but if it does, we can assume the thread is deleted
        if (!channel || !channel?.isTextBased() || !channel?.isThread()) {
          console.error(
            "Channel either, not found, not text-based, or not a thread in general",
          );
          break;
        }

        // Want to store every single message sent in that channel
        let allMessages: {
          user: string;
          message: string;
        }[] = [];

        // Store the last message processed
        let lastId: string | undefined;

        // Paginate through the messages to fetch all the messages
        while (true) {
          const options: { limit: number; before?: string } = { limit: 100 };
          if (lastId) {
            options.before = lastId;
          }

          const messages = await channel.messages.fetch(options);

          if (messages.size === 0) {
            break;
          }

          // Concat all the messages into one array
          allMessages = allMessages.concat(
            messages.map((msg) => ({
              user: msg.author.username,
              message: msg.content,
            })),
          );

          // Store the last message id for the next iteration
          lastId = messages.last()?.id;
        }

        console.log(`Fetched ${allMessages.length} messages`);

        const threadTitle = channel.name || "Untitled Thread";
        const prompt = prompts.getDiscordThreadsPrompt(
          allMessages,
          threadTitle,
        );

        const response = await llm.generateResponse(
          prompt.system,
          prompt.human,
        );

        await UpsertDocument(
          `thread-${threadId}`,
          response,
          `https://discord.com/channels/${channel.guild.id}/${channel.id}`,
        );

        try {
          console.log(`Trained on ${channel.url}`);
          // await (
          //   client.channels.cache.get(env.LoggingChannelId) as TextChannel
          // ).send({
          //   content: `Trained on ${channel.url}`,
          // });
        } catch (err) {
          console.error("Failed to send log message:", err);
        }
        alreadyTrainedThreads.push(threadId);
      }

      // After training, update the database with the new threads
      await globalThis.databases.trainedThreads.put(
        forum,
        alreadyTrainedThreads,
      );
    }
  } catch (error) {
    console.error("Error training on discord threads:", error);
    return false;
  }
}
