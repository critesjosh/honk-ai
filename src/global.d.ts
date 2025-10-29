/* eslint-disable no-var */
import { RootDatabase } from "lmdb";

declare global {
  var databases: {
    documents: RootDatabase;
    analytics: RootDatabase;
    responses: RootDatabase;
    questions: RootDatabase;
    trainedThreads: RootDatabase;
    trainedIssues: RootDatabase;
    releaseTracking: RootDatabase;
    mcpTokens: RootDatabase;
    mcpRequests: RootDatabase;
  };
}

export {};
