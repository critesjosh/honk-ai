import { ButtonInteraction, ModalSubmitInteraction, ChatInputCommandInteraction } from "discord.js";
import { CustomEmbed } from "../utils/embedBuilder.js";
import { processAnalyticsQuery } from "../utils/analytics.js";
import { chunkLLMResponse } from "../utils/responseFormatter.js";
import { showAnalyticsModal } from "../commands/analytics.js";

export const handleButtonInteraction = async (interaction: ButtonInteraction) => {
  try {
    // Handle analytics modal button
    if (interaction.customId === 'open_analytics_modal') {
      await showAnalyticsModal(interaction);
      return;
    }
    
    // Handle rating buttons
    if (!interaction.customId.startsWith('rating-')) return;

    const isGood = interaction.customId.includes('good');
    const responseId = interaction.customId.split('-').pop();

    if (!responseId) return;

    // Acknowledge the interaction
    await interaction.reply({
      embeds: [
        new CustomEmbed().setDescription(
          isGood 
            ? "Thanks for the positive feedback! 👍"
            : "Thanks for the feedback. We'll work on improving! 👎"
        )
      ],
      ephemeral: true
    });

    // Update the response in the database with the rating
    try {
      const existingResponse = globalThis.databases?.responses?.get(responseId);
      if (existingResponse) {
        const updatedResponse = {
          ...existingResponse,
          rating: {
            type: isGood ? 'good' : 'bad',
            response: undefined,
            user_id: interaction.user.id,
            timestamp: Date.now()
          }
        };
        
        globalThis.databases?.responses?.put(responseId, updatedResponse);
        console.log(`Updated response ${responseId} with rating: ${isGood ? 'good' : 'bad'}`);
      }
    } catch (dbError) {
      console.error('Error updating response in database:', dbError);
    }

    // Update analytics counters
    try {
      const currentGoodCount = parseInt(globalThis.databases?.analytics?.get("totalGoodRatings") || "0");
      const currentBadCount = parseInt(globalThis.databases?.analytics?.get("totalBadRatings") || "0");
      
      if (isGood) {
        globalThis.databases?.analytics?.put("totalGoodRatings", (currentGoodCount + 1).toString());
        console.log(`New total good ratings: ${currentGoodCount + 1}`);
      } else {
        globalThis.databases?.analytics?.put("totalBadRatings", (currentBadCount + 1).toString());
        console.log(`New total bad ratings: ${currentBadCount + 1}`);
      }

      // Also update total ratings
      const totalRatings = currentGoodCount + currentBadCount + 1;
      globalThis.databases?.analytics?.put("totalRatings", totalRatings.toString());
      
    } catch (analyticsError) {
      console.error('Error updating analytics:', analyticsError);
    }

  } catch (error) {
    console.error('Error handling button interaction:', error);
  }
};


export const handleModalSubmit = async (interaction: ModalSubmitInteraction) => {
  try {
    if (interaction.customId === 'analytics_modal') {
      // Get the form values
      const startDate = interaction.fields.getTextInputValue('start_date');
      const endDate = interaction.fields.getTextInputValue('end_date');
      const question = interaction.fields.getTextInputValue('analytics_question');

      // Validate date format
      const dateRegex = /^\d{4}-\d{2}-\d{2}$/;
      if (!dateRegex.test(startDate) || !dateRegex.test(endDate)) {
        await interaction.reply({
          embeds: [
            new CustomEmbed()
              .setDescription("❌ Invalid date format. Please use YYYY-MM-DD format.")
              .setRed()
          ],
          ephemeral: true
        });
        return;
      }

      // Convert dates to timestamps
      const startTimestamp = new Date(startDate + 'T00:00:00Z').getTime();
      const endTimestamp = new Date(endDate + 'T23:59:59Z').getTime();

      // Validate date range
      if (startTimestamp >= endTimestamp) {
        await interaction.reply({
          embeds: [
            new CustomEmbed()
              .setDescription("❌ Start date must be before end date.")
              .setRed()
          ],
          ephemeral: true
        });
        return;
      }

      // Show loading response
      await interaction.reply({
        embeds: [
          new CustomEmbed()
            .setDescription("🔍 Analyzing questions... This may take a moment.")
        ],
        ephemeral: true
      });

      try {
        // Process the analytics query
        const analyticsResponse = await processAnalyticsQuery(question, {
          start: startTimestamp,
          end: endTimestamp
        });

        // Split response if too long for embed
        const maxLength = 3500;
        if (analyticsResponse.length <= maxLength) {
          const embed = new CustomEmbed()
            .setTitle("📊 Question Analytics Results")
            .setDescription(analyticsResponse)
            .addFields(
              { name: "📅 Date Range", value: `${startDate} to ${endDate}`, inline: true },
              { name: "❓ Your Question", value: question.length > 100 ? question.substring(0, 97) + "..." : question, inline: false }
            );

          await interaction.editReply({ embeds: [embed] });
        } else {
          // Split into multiple embeds
          const chunks = chunkLLMResponse(analyticsResponse, maxLength);
          
          // First embed with title and metadata
          const firstEmbed = new CustomEmbed()
            .setTitle("📊 Question Analytics Results")
            .setDescription(chunks[0])
            .addFields(
              { name: "📅 Date Range", value: `${startDate} to ${endDate}`, inline: true },
              { name: "❓ Your Question", value: question.length > 100 ? question.substring(0, 97) + "..." : question, inline: false }
            );

          await interaction.editReply({ embeds: [firstEmbed] });

          // Additional embeds for remaining chunks
          for (let i = 1; i < chunks.length; i++) {
            const embed = new CustomEmbed().setDescription(chunks[i]);
            await interaction.followUp({ embeds: [embed], ephemeral: true });
          }
        }

      } catch (error) {
        console.error("Error processing analytics modal:", error);
        await interaction.editReply({
          embeds: [
            new CustomEmbed()
              .setDescription("❌ Sorry, I encountered an error while processing your analytics request.")
              .setRed()
          ]
        });
      }
    }
  } catch (error) {
    console.error('Error handling modal submit:', error);
  }
};

export default {
  handleButtonInteraction,
  handleModalSubmit,
};