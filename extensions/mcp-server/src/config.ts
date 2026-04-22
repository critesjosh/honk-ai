export interface Config {
  apiUrl: string;
  apiKey: string;
  requestTimeout: number;
}

export function loadConfig(): Config {
  const apiKey = process.env.API_KEY;
  if (!apiKey) {
    console.error(
      "Error: API_KEY environment variable is required.\n" +
        "Get your key by running /mcp-key in the Noir Discord,\n" +
        "then set API_KEY in your MCP client configuration."
    );
    process.exit(1);
  }

  return {
    apiUrl: process.env.API_URL || "http://localhost:7091",
    apiKey,
    requestTimeout: parseInt(process.env.REQUEST_TIMEOUT || "60000", 10),
  };
}
