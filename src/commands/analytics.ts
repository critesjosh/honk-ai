import { 
  ModalBuilder, 
  TextInputBuilder, 
  TextInputStyle, 
  ActionRowBuilder,
  ButtonInteraction,
  ButtonBuilder,
  ButtonStyle
} from "discord.js";
import { CustomEmbed } from "../utils/embedBuilder.js";

export function createAnalyticsEmbed() {
  const embed = new CustomEmbed()
    .setTitle("📊 Question Analytics")
    .setDescription("Click the button below to analyze user questions from the support bot. You can specify a date range and ask questions about patterns, trends, and common issues.")
    .addFields(
      { name: "📅 Date Range", value: "Specify start and end dates in YYYY-MM-DD format", inline: false },
      { name: "❓ Analytics Query", value: "Ask questions like:\n• What are the most common questions?\n• What patterns do you see in user issues?\n• What topics need better documentation?", inline: false }
    );

  const button = new ButtonBuilder()
    .setCustomId('open_analytics_modal')
    .setLabel('📊 Query Analytics')
    .setStyle(ButtonStyle.Primary);

  const row = new ActionRowBuilder<ButtonBuilder>().addComponents(button);

  return { embed, components: [row] };
}

export async function showAnalyticsModal(interaction: ButtonInteraction) {
  // Create the modal
  const modal = new ModalBuilder()
    .setCustomId('analytics_modal')
    .setTitle('Question Analytics Query');

  // Create the text inputs
  const startDateInput = new TextInputBuilder()
    .setCustomId('start_date')
    .setLabel('Start Date (YYYY-MM-DD)')
    .setStyle(TextInputStyle.Short)
    .setPlaceholder('2024-01-01')
    .setRequired(true)
    .setMaxLength(10)
    .setMinLength(10);

  const endDateInput = new TextInputBuilder()
    .setCustomId('end_date')
    .setLabel('End Date (YYYY-MM-DD)')
    .setStyle(TextInputStyle.Short)
    .setPlaceholder('2024-01-31')
    .setRequired(true)
    .setMaxLength(10)
    .setMinLength(10);

  const questionInput = new TextInputBuilder()
    .setCustomId('analytics_question')
    .setLabel('Your Analytics Question')
    .setStyle(TextInputStyle.Paragraph)
    .setPlaceholder('What are the most common questions users are asking? What patterns do you see?')
    .setRequired(true)
    .setMaxLength(1000);

  // Add inputs to action rows
  const firstActionRow = new ActionRowBuilder<TextInputBuilder>().addComponents(startDateInput);
  const secondActionRow = new ActionRowBuilder<TextInputBuilder>().addComponents(endDateInput);
  const thirdActionRow = new ActionRowBuilder<TextInputBuilder>().addComponents(questionInput);

  // Add rows to the modal
  modal.addComponents(firstActionRow, secondActionRow, thirdActionRow);

  // Show the modal to the user
  await interaction.showModal(modal);
}