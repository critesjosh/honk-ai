import { UserQuestion } from "../types/data.js";
import { generateResponse } from "./llm.js";
import prompts from "../config/prompts.js";

function getAllQuestionsInTimeframe(
  start: number,
  end: number,
): UserQuestion[] {
  const questions: UserQuestion[] = [];

  if (!globalThis.databases?.questions) {
    return questions;
  }

  const db = globalThis.databases.questions;

  for (const entry of db.getRange()) {
    const userQuestion = entry.value as UserQuestion;
    if (userQuestion.timestamp >= start && userQuestion.timestamp <= end) {
      questions.push(userQuestion);
    }
  }

  return questions;
}

export function storeUserQuestion(
  userId: string,
  userName: string,
  question: string,
): void {
  if (!globalThis.databases?.questions) {
    console.error("Questions database not available");
    return;
  }

  const questionId = `${userId}-${Date.now()}`;
  const userQuestion: UserQuestion = {
    id: questionId,
    user_id: userId,
    user_name: userName,
    question: question,
    timestamp: Date.now(),
  };

  try {
    globalThis.databases.questions.put(questionId, userQuestion);
    console.log(
      `Stored question from user ${userName}: ${question.substring(0, 50)}...`,
    );
  } catch (error) {
    console.error("Error storing user question:", error);
  }
}

export async function processAnalyticsQuery(
  userQuestion: string,
  timeframe: { start: number; end: number },
): Promise<string> {
  const questions = getAllQuestionsInTimeframe(timeframe.start, timeframe.end);

  if (questions.length === 0) {
    return "No questions found in the specified timeframe.";
  }

  const questionsList = questions
    .map(
      (question, index) =>
        `${index + 1}. "${question.question}" (User: ${question.user_name})`,
    )
    .join("\n");

  const promptData = prompts.getAnalyticsPrompt(
    questionsList,
    questions.length,
    new Set(questions.map((q) => q.user_id)).size,
    new Date(timeframe.start).toLocaleDateString(),
    new Date(timeframe.end).toLocaleDateString(),
    userQuestion,
  );

  console.log(promptData);

  try {
    const response = await generateResponse(
      promptData.system,
      promptData.human,
    );
    return response;
  } catch (error) {
    console.error("Error generating analytics insight:", error);
    return `Found ${questions.length} questions from ${new Set(questions.map((q) => q.user_id)).size} unique users in the timeframe ${new Date(timeframe.start).toLocaleDateString()} to ${new Date(timeframe.end).toLocaleDateString()}. Unable to generate detailed analysis due to an error.`;
  }
}
