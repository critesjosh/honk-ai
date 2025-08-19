import {
  Client,
  Message,
  OmitPartialGroupDMChannel,
  ActionRowBuilder,
  ButtonBuilder,
  ButtonStyle,
} from "discord.js";

import {
  UserMessages,
  Conversation,
  BotResponse,
  Prompt,
} from "../types/data.js";
import { env } from "../env.js";
import { CustomEmbed } from "../utils/embedBuilder.js";
import { invokeRagChain } from "../utils/llm.js";
import {
  chunkLLMResponse,
  formatSourcesForDiscord,
} from "../utils/responseFormatter.js";
import { generateEmbedding } from "../utils/embeddings.js";
import { createAnalyticsEmbed } from "../commands/analytics.js";
import { storeUserQuestion } from "../utils/analytics.js";

// To combat spam, we need to keep track of the last messages sent by a user to prevent them from spamming
const userMessages = new Map<string, UserMessages>();

// So it doesnt fill up with old data, we want to clean it up every minute
setInterval(cleanupInactiveUsers, 60000);

// Discord embed description hard limit
const EMBED_DESC_LIMIT = 4096;

interface MessageData {
  embeds: CustomEmbed[];
  components: ActionRowBuilder<ButtonBuilder>[];
  ID: string;
}

export const newMessage = async (
  client: Client,
  message: OmitPartialGroupDMChannel<Message>,
) => {
  try {
    if (message.author.bot) return;

    // Make the sure the message is in the correct channel, and includes the bot mention
    // For threads, check the parent channel ID instead of the thread ID
    const channelId = message.channel.isThread()
      ? message.channel.parentId
      : message.channel.id;
    if (!channelId) {
      return;
    }

    // Check if this is the analytics channel
    const isAnalyticsChannel = channelId === env.AnalyticsChannelId;

    // Ensure the message is coming from an allowed channel or analytics channel
    if (!env.AllowedChannelIds.includes(channelId) && !isAnalyticsChannel)
      return;

    // Handle analytics channel first - don't create thread or require bot mention
    if (isAnalyticsChannel) {
      const { embed, components } = createAnalyticsEmbed();
      await message.channel.send({ embeds: [embed], components });

      console.log(
        `Analytics interface shown to user: ${message.author.username}`,
      );
      return;
    }

    // In threads, no bot mention required. In regular channels, bot mention is required.
    const requiresBotMention = !message.channel.isThread();
    if (
      requiresBotMention &&
      !message.content.toLowerCase().includes(`<@${env.BOT_ID}>`)
    ) {
      return;
    }

    console.log("Query Received");

    // This returns the number of requests a user has made in the last minute
    const UserSpamData: UserMessages | undefined = userMessages.get(
      message.author.id,
    );

    // If not in a thread, create one and use that for the response
    let threadMessage = message;
    if (!message.channel.isThread()) {
      const thread = await message.startThread({
        name: `${message.author.username}'s Question`,
        autoArchiveDuration: 1440,
      });

      threadMessage = await thread.send({
        embeds: [
          new CustomEmbed().setDescription(
            "Honk is a helpful AI assistant, but generated code may be incomplete, insecure, or incorrect.\n- Be specific and prefer step-by-step requests so Honk can help the best and use its context the best.\n- Not reliable for one-shot generation of full contracts.\n- Always review, test, and audit before use. Never share secrets.\n\nI will remember the topic of this thread, and the 6 most recent messages.",
          ),
        ],
      });
    }

    // Make it appear as if the bot is typing out her response
    // The "typing" indicator goes away after 5 seconds, so if the message isnt sent by then, we should the typing indicator again
    threadMessage.channel.sendTyping();
    const typingInterval = setInterval(
      () => threadMessage.channel.sendTyping(),
      5000,
    );

    // If the user already exists in the map we want to increment the requests
    if (UserSpamData) {
      UserSpamData.requests++;

      // If the user has made too many requests in the last minute, we want to block them
      if (UserSpamData.requests > env.RequestsPerMinute) {
        clearInterval(typingInterval);
        await message.reply({
          embeds: [
            new CustomEmbed()
              .setDescription(
                `You have made too many requests in the last minute, please wait a minute before trying again.`,
              )
              .setRed(),
          ],
        });
        return;
      }
    } else {
      // If the user doesnt exist in the map we want to create it
      userMessages.set(message.author.id, {
        expirationTime: Date.now() + 60000,
        requests: 1,
      });
    }

    try {
      // Extract the prompt by removing the bot mention
      const prompt = message.content
        .replace(new RegExp(`<@!?${env.BOT_ID}>`, "gi"), "")
        .trim();

      // Get chat history if in a thread
      let chatHistory: Conversation = [];
      if (threadMessage.channel.isThread()) {
        // Get the thread starter message
        const starterMessage =
          await threadMessage.channel.fetchStarterMessage();

        // Fetch last messages from thread
        const messages = await threadMessage.channel.messages.fetch({
          limit: env.MessageHistoryPerThread + 1,
        });

        // Add starter message if it exists and isn't already in the messages collection
        if (starterMessage && !messages.has(starterMessage.id)) {
          messages.set(starterMessage.id, starterMessage);
        }

        // Convert messages to chat history format
        const historyMessages = messages
          .sort((a, b) => a.createdTimestamp - b.createdTimestamp)
          .filter((msg) => msg.id !== message.id)
          .filter((msg) => {
            const firstEmbed = msg.embeds?.[0];
            if (firstEmbed?.description) {
              const desc = firstEmbed.description;
              if (
                desc.includes(
                  "You have made too many requests in the last minute",
                )
              ) {
                return false;
              }
              if (
                desc.includes(
                  "I will remember the topic of this thread, and the 6 most recent messages.",
                )
              ) {
                return false;
              }
            }
            return true;
          })
          .filter((msg) => {
            // Exclude other bots; only include our bot as assistant or real users
            if (msg.author.bot && msg.author.id !== env.BOT_ID) {
              return false;
            }
            return true;
          })
          .map((msg) => ({
            who:
              msg.author.id === env.BOT_ID
                ? ("assistant" as const)
                : ("user" as const),
            content:
              msg.author.id === env.BOT_ID
                ? msg.embeds[0]?.description || msg.content
                : msg.content
                    .replace(new RegExp(`<@!?${env.BOT_ID}>`, "gi"), "")
                    .trim(),
          }))
          .filter((msg) => msg.content);

        // Merge consecutive messages by the same speaker into a single turn
        const mergedTurns: Conversation = [];
        for (const turn of historyMessages) {
          const last = mergedTurns[mergedTurns.length - 1];
          if (last && last.who === turn.who) {
            last.content = `${last.content}\n\n${turn.content}`;
          } else {
            mergedTurns.push({ who: turn.who, content: turn.content });
          }
        }

        // Keep only the last 4 merged turns
        chatHistory = mergedTurns.slice(-4);
      }

      // Store the user question for analytics
      storeUserQuestion(message.author.id, message.author.username, prompt);

      // Query the RAG system
      const ragResponse = await invokeRagChain(prompt, chatHistory);

      // Calculate space needed for sources
      const sourcesText = formatSourcesForDiscord(ragResponse.sources);
      const sourcesLength = sourcesText.length;

      // Chunk the response using the embed description limit
      const chunkedResponse = chunkLLMResponse(
        ragResponse.answer,
        EMBED_DESC_LIMIT,
      );

      // Clear the typing indicator
      clearInterval(typingInterval);

      // Object to store the data for the message
      const MessageData: MessageData = {
        embeds: [],
        components: [],
        ID: "",
      };

      // Create a unique ID for the response
      const ID = Date.now().toString();
      MessageData.ID = ID;

      // Process each chunk and create embeds
      chunkedResponse.forEach((chunk, i) => {
        const embed = new CustomEmbed();
        let finalChunk = chunk;

        // Add sources to the last chunk, but check if it fits
        if (i + 1 === chunkedResponse.length) {
          // Prepare metadata text (provider/model/tokens)
          const meta: string[] = [];
          if (ragResponse.provider)
            meta.push(`Provider: ${ragResponse.provider}`);
          if (ragResponse.model) meta.push(`Model: ${ragResponse.model}`);
          const u: any = ragResponse.usage || {};
          const tokenParts: string[] = [];
          if (u.promptTokens ?? u.inputTokens)
            tokenParts.push(`prompt=${u.promptTokens ?? u.inputTokens}`);
          if (u.completionTokens ?? u.outputTokens)
            tokenParts.push(
              `completion=${u.completionTokens ?? u.outputTokens}`,
            );
          if (u.totalTokens) tokenParts.push(`total=${u.totalTokens}`);
          const metaText =
            meta.length || tokenParts.length
              ? `\n\n-----\n${meta.join(" | ")}${
                  tokenParts.length ? ` | Tokens: ${tokenParts.join(", ")}` : ""
                }`
              : "";

          // Try to append both sources and meta if they fit
          const combined = `${chunk}${sourcesText}`;
          if (combined.length <= EMBED_DESC_LIMIT) {
            finalChunk = combined;
          } else {
            // Sources don't fit, create a separate embed for them
            // Ensure the sources embed respects the limit
            const maxSourcesLen = EMBED_DESC_LIMIT;
            if (sourcesText.length <= maxSourcesLen) {
              const sourcesEmbed = new CustomEmbed().setDescription(
                `${sourcesText}`,
              );
              MessageData.embeds.push(sourcesEmbed);
            } else {
              const truncatedSources =
                sourcesText.substring(0, EMBED_DESC_LIMIT - 13) +
                "... (truncated)";
              const sourcesEmbed = new CustomEmbed().setDescription(
                truncatedSources,
              );
              MessageData.embeds.push(sourcesEmbed);
            }

            // If metadata exists, create another small embed for it
            // if (metaText) {
            //   let safeMeta = metaText;
            //   if (safeMeta.length > EMBED_DESC_LIMIT) {
            //     safeMeta =
            //       safeMeta.substring(0, EMBED_DESC_LIMIT - 13) +
            //       "... (truncated)";
            //   }
            //   const metaEmbed = new CustomEmbed().setDescription(safeMeta);
            //   MessageData.embeds.push(metaEmbed);
            // }
          }

          // Create feedback buttons (only on the last embed)
          const thumbsUp = new ButtonBuilder()
            .setCustomId(`rating-good-${ID}`)
            .setLabel("👍")
            .setStyle(ButtonStyle.Success);

          const thumbsDown = new ButtonBuilder()
            .setCustomId(`rating-bad-${ID}`)
            .setLabel("👎")
            .setStyle(ButtonStyle.Danger);

          const row = new ActionRowBuilder<ButtonBuilder>().addComponents(
            thumbsUp,
            thumbsDown,
          );

          MessageData.components.push(row);
        }

        // Extra safety check - ensure no chunk exceeds the limit
        if (finalChunk.length > EMBED_DESC_LIMIT) {
          finalChunk =
            finalChunk.substring(0, EMBED_DESC_LIMIT - 13) + "... (truncated)";
        }

        console.log(`Embed ${i + 1} length: ${finalChunk.length} characters`);
        embed.setDescription(finalChunk);
        MessageData.embeds.push(embed);
      });

      // Send the response - one embed per message
      let botReply;

      for (let i = 0; i < MessageData.embeds.length; i++) {
        const embed = MessageData.embeds[i];
        const isLastMessage = i === MessageData.embeds.length - 1;

        const messageOptions: any = {
          embeds: [embed],
        };

        // Only include components on the very last message
        if (isLastMessage && MessageData.components.length > 0) {
          messageOptions.components = MessageData.components;
        }

        if (i === 0) {
          // First message is a reply
          botReply = await threadMessage.reply(messageOptions);
        } else {
          // Subsequent messages are follow-ups
          await threadMessage.channel.send(messageOptions);
        }
      }

      console.log(`Response sent for user: ${message.author.username}`);

      // Extract header from response and rename thread if this is a new thread
      if (!message.channel.isThread() && threadMessage.channel.isThread()) {
        try {
          const firstLine = ragResponse.answer.split('\n')[0].trim();
          if (firstLine.startsWith('#')) {
            const headerText = firstLine.replace(/^#+\s*/, '').trim();
            if (headerText && headerText.length <= 100) {
              await threadMessage.channel.setName(headerText);
            }
          }
        } catch (headerError) {
          console.error("Error extracting header for thread rename:", headerError);
        }
      }

      // Store the response in the database if we have a unique ID and botReply exists
      if (MessageData.ID && botReply) {
        try {
          // Generate embedding for the prompt for storage
          const promptEmbedding = await generateEmbedding(prompt);

          const promptData: Prompt = {
            embedding: promptEmbedding,
            text: prompt,
          };

          const responseData: BotResponse = {
            prompt: promptData,
            answer: ragResponse.answer,
            response_id: MessageData.ID,
            response_message_id: botReply.id,
            user_id: message.author.id,
            user_name: message.author.username,
            sources: ragResponse.sources,
            date: Date.now(),
            rating: {
              type: "none",
              response: undefined,
            },
          };

          globalThis.databases?.responses?.put(MessageData.ID, responseData);

          // Update total questions counter
          const currentQuestions = parseInt(
            globalThis.databases?.analytics?.get("totalQuestions") || "0",
          );
          globalThis.databases?.analytics?.put(
            "totalQuestions",
            (currentQuestions + 1).toString(),
          );

          console.log(`Stored response ${MessageData.ID} in database`);
          console.log(`New total questions: ${currentQuestions + 1}`);
        } catch (storageError) {
          console.error("Error storing response in database:", storageError);
        }
      }
    } catch (error) {
      clearInterval(typingInterval);
      console.error("Error processing message:", error);

      await threadMessage.reply({
        embeds: [
          new CustomEmbed()
            .setDescription(
              "Sorry, I encountered an error while processing your request. Please try again later.",
            )
            .setRed(),
        ],
      });
    }
  } catch (error) {
    console.error("Error in message handler:", error);
  }
};

function cleanupInactiveUsers() {
  const now = Date.now();
  for (const [userId, data] of userMessages.entries()) {
    if (now > data.expirationTime) {
      userMessages.delete(userId);
    }
  }
}
