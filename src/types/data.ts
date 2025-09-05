export type SimilaritySearchResponse = {
  ids: string[];
  embeddings: number[][];
  documents: string[];
  metadatas: (Record<string, string | number | boolean> | null)[][];
  distances: number[][] | null;
};

export type Prompt = {
  embedding: number[];
  text: string;
};

export type Conversation = {
  who: "user" | "assistant";
  content: string;
}[];

export type UserMessages = {
  expirationTime: number;
  requests: number;
};

export type BotResponse = {
  prompt: Prompt;
  answer: string;
  response_id: string;
  response_message_id: string;
  user_id: string;
  user_name: string;
  sources: string[];
  date: number;
  rating: {
    type: "none" | "good" | "bad";
    response?: string;
    user_id?: string;
    timestamp?: number;
  };
};

export type AnalyticsQuery = {
  type: "most_asked_questions" | "question_trends" | "user_activity";
  timeframe: {
    start: number;
    end: number;
  };
  limit?: number;
};

export type QuestionFrequency = {
  question: string;
  count: number;
  users: string[];
  firstAsked: number;
  lastAsked: number;
};

export type AnalyticsResponse = {
  query: AnalyticsQuery;
  results: QuestionFrequency[];
  totalQuestions: number;
  uniqueUsers: number;
};

export type UserQuestion = {
  id: string;
  user_id: string;
  user_name: string;
  question: string;
  timestamp: number;
};
