import fs from "fs/promises";
import path from "path";
import { env } from "../../../env.js";
import trainingConfig from "../../../config/training.json" with { type: "json" };
import { getLatestAztecRelease } from "../../../utils/github.js";

/**
 * Get line number from character index in file content
 */
function getLineNumberFromIndex(fileContent: string, index: number): number {
  return fileContent.substring(0, index).split("\n").length;
}

/**
 * Process highlighting comments for a specific identifier
 */
function processHighlighting(codeSnippet: string, identifier: string): string {
  const lines = codeSnippet.split("\n");
  const regex1 = /highlight-next-line:([a-zA-Z0-9-._:]+)/;
  const replacement1 = "highlight-next-line";
  const regex2 = /highlight-start:([a-zA-Z0-9-._:]+)/;
  const replacement2 = "highlight-start";
  const regex3 = /highlight-end:([a-zA-Z0-9-._:]+)/;
  const replacement3 = "highlight-end";
  const regex4 = /this-will-error:([a-zA-Z0-9-._:]+)/;
  const replacement4 = "this-will-error";

  let mutated = false;

  const processLine = (
    line: string,
    regex: RegExp,
    replacement: string,
  ): string => {
    const match = line.match(regex);
    if (match) {
      mutated = true;
      const identifiers = match[1].split(":");
      if (identifiers.includes(identifier)) {
        line = line.replace(match[0], replacement);
      } else {
        // Remove matched text completely
        line = line.replace(match[0], "");
      }
    }
    return line.trim() === "//" || line.trim() === "#" ? "" : line;
  };

  const countLeadingSpaces = (line: string): number => {
    const match = line.match(/^ */);
    return match ? match[0].length : 0;
  };

  let indention = 200;
  let resultLines: string[] = [];

  for (let line of lines) {
    mutated = false;
    line = processLine(line, regex1, replacement1);
    line = processLine(line, regex2, replacement2);
    line = processLine(line, regex3, replacement3);
    line = processLine(line, regex4, replacement4);

    if (!(line === "" && mutated)) {
      resultLines.push(line);
      const leadingSpaces = countLeadingSpaces(line);
      if (line.length > 0 && leadingSpaces < indention) {
        indention = leadingSpaces;
      }
    }
  }

  let result = "";
  for (let line of resultLines) {
    result +=
      (line.length > indention ? line.substring(indention) : line).trimEnd() +
      "\n";
  }
  return result.trimEnd();
}

/**
 * Extract code snippet between docs:start and docs:end markers
 */
async function extractCodeSnippet(
  filePath: string,
  identifier: string,
): Promise<[string, number, number]> {
  const fileContent = await fs.readFile(filePath, "utf-8");
  let linesToRemove: number[] = [];

  const startRegex = /(?:\/\/|#)\s+docs:start:([a-zA-Z0-9-._:]+)/g;
  const endRegex = /(?:\/\/|#)\s+docs:end:([a-zA-Z0-9-._:]+)/g;

  const lookForMatch = (
    regex: RegExp,
  ): [RegExpExecArray | null, number | null] => {
    let match;
    let matchFound = false;
    let matchedLineNum: number | null = null;
    let actualMatch: RegExpExecArray | null = null;
    const lines = fileContent.split("\n");

    // Reset regex lastIndex
    regex.lastIndex = 0;

    while ((match = regex.exec(fileContent))) {
      if (match !== null) {
        const identifiers = match[1].split(":");
        let tempMatch = identifiers.includes(identifier) ? match : null;

        if (tempMatch === null) {
          // Mark lines for removal from other identifiers
          for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            if (line.trim() === match[0].trim()) {
              linesToRemove.push(i + 1); // lines are indexed from 1
            }
          }
        } else {
          if (matchFound) {
            throw new Error(
              `Duplicate for regex ${regex} and identifier ${identifier}`,
            );
          }
          matchFound = true;
          matchedLineNum = getLineNumberFromIndex(fileContent, tempMatch.index);
          actualMatch = tempMatch;
        }
      }
    }

    return [actualMatch, matchedLineNum];
  };

  const [startMatch, startLineNum] = lookForMatch(startRegex);
  const [endMatch, endLineNum] = lookForMatch(endRegex);

  if (startMatch === null || endMatch === null) {
    if (startMatch === null && endMatch === null) {
      throw new Error(
        `Identifier "${identifier}" not found in file "${filePath}"`,
      );
    } else if (startMatch === null) {
      throw new Error(
        `Start line "docs:start:${identifier}" not found in file "${filePath}"`,
      );
    } else {
      throw new Error(
        `End line "docs:end:${identifier}" not found in file "${filePath}"`,
      );
    }
  }

  let lines = fileContent.split("\n");

  // Filter lines to remove within bounds
  linesToRemove = linesToRemove.filter((lineNum) => {
    return lineNum >= startLineNum! && lineNum <= endLineNum!;
  });

  // Remove lines with docs comments for unrelated identifiers
  lines = lines.filter((_, i) => {
    return !linesToRemove.includes(i + 1);
  });

  // Extract lines between start and end
  lines = lines.filter((_, i) => {
    return i + 1 > startLineNum! && i + 1 < endLineNum! - linesToRemove.length;
  });

  let codeSnippet = lines.join("\n");
  codeSnippet = processHighlighting(codeSnippet, identifier);

  return [codeSnippet, startLineNum!, endLineNum!];
}

/**
 * Process #include_code directives using the Aztec system
 */
export async function processIncludeCodeDirectives(
  markdownContent: string,
  repoPath: string,
): Promise<string> {
  // Match directive with optional extra options after language
  // Format: #include_code identifier /path/to/file language [options...]
  const regex = /^\s*#include_code\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(.*))?\s*$/gm;

  let updatedContent = markdownContent;
  let match: RegExpExecArray | null;

  // Helper to determine if match is inside a fenced code block
  const isInsideCodeFence = (index: number): boolean => {
    const upto = markdownContent.substring(0, index);
    const fenceMatches = upto.match(/^```.*$/gm);
    return fenceMatches ? fenceMatches.length % 2 === 1 : false;
  };

  // Reset regex lastIndex for global matching
  regex.lastIndex = 0;

  while ((match = regex.exec(markdownContent))) {
    const fullMatch = match[0];
    const identifier = match[1];
    let codeFilePath = match[2];
    let language = match[3];
    const optsStr = (match[4] || "").trim();

    // Handle absolute paths (treat as repo-root relative)
    if (codeFilePath.startsWith("/")) {
      codeFilePath = codeFilePath.substring(1);
    }

    const noSourceLink = optsStr.includes("noSourceLink");

    try {
      const absCodeFilePath = path.join(repoPath, codeFilePath);

      // Check if file exists
      try {
        await fs.access(absCodeFilePath);
      } catch {
        console.warn(`Could not find file for #include_code: ${codeFilePath}`);
        const placeholder = `\`\`\`${language}
// File not found: ${codeFilePath}
// Identifier: ${identifier}
\`\`\``;
        updatedContent = updatedContent.replace(fullMatch, placeholder);
        continue;
      }

      // Get code snippet
      let codeSnippet = "";
      if (language.toLowerCase() === "raw") {
        // Include entire file content for raw
        codeSnippet = await fs.readFile(absCodeFilePath, "utf-8");
        // Determine display language from path
        language = detectLanguageFromPath(codeFilePath);
      } else {
        try {
          const [snippet] = await extractCodeSnippet(
            absCodeFilePath,
            identifier,
          );
          codeSnippet = snippet;
        } catch (e) {
          // Fallback: include whole file content if markers missing
          codeSnippet = await fs.readFile(absCodeFilePath, "utf-8");
        }
      }

      // Build replacement
      const tag = commitTagFromTrainingConfig();
      const urlText = `${codeFilePath}#L1-L${codeSnippet.split("\n").length}`;
      const url = `https://github.com/AztecProtocol/aztec-packages/blob/${tag}/${urlText}`;

      const insideFence = isInsideCodeFence(match.index);
      let replacement = "";

      if (insideFence) {
        // Already inside a fenced code block; insert raw snippet
        replacement = codeSnippet.trimEnd();
        // No source link inside fenced blocks to avoid breaking formatting
      } else {
        // Not inside a fence; create one, preserve any provided options
        const optsForFence = optsStr
          .split(/\s+/)
          .filter((opt) => opt && opt !== "noSourceLink")
          .join(" ");
        const optSuffix = optsForFence ? ` ${optsForFence}` : "";
        replacement = `\`\`\`${language}${optSuffix}\n${codeSnippet.trimEnd()}\n\`\`\``;
        if (!noSourceLink) {
          replacement += `\n> <sup><sub><a href=\"${url}\" target=\"_blank\" rel=\"noopener noreferrer\">Source code: ${urlText}</a></sub></sup>`;
        }
      }

      updatedContent = updatedContent.replace(fullMatch, replacement);
      console.log(
        `Included code snippet \"${identifier}\" from: ${codeFilePath}`,
      );
    } catch (error) {
      const lineNum = getLineNumberFromIndex(markdownContent, match.index);
      console.error(
        `Error processing #include_code at line ${lineNum}:`,
        error,
      );

      // Create error placeholder
      const placeholder = `\`\`\`${language}
// Error extracting code snippet: ${identifier}
// File: ${codeFilePath}
// Error: ${error instanceof Error ? error.message : "Unknown error"}
\`\`\``;
      updatedContent = updatedContent.replace(fullMatch, placeholder);
    }
  }

  return updatedContent;
}

function commitTagFromTrainingConfig(): string {
  try {
    const aztecRepo = (trainingConfig.repositories || []).find(
      (r: any) => r.name === "aztec-packages" && r.org === "AztecProtocol",
    );
    if (
      aztecRepo &&
      typeof aztecRepo.branch === "string" &&
      aztecRepo.branch.length > 0
    ) {
      return aztecRepo.branch;
    }
  } catch {}
  return "master";
}

/**
 * Detect programming language from file path
 */
function detectLanguageFromPath(filePath: string): string {
  const extension = path.extname(filePath).toLowerCase();

  const languageMap: Record<string, string> = {
    ".nr": "noir",
    ".rs": "rust",
    ".ts": "typescript",
    ".js": "javascript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".py": "python",
    ".sol": "solidity",
    ".md": "markdown",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "bash",
    ".dockerfile": "dockerfile",
    ".go": "go",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
  };

  return languageMap[extension] || "text";
}

/**
 * Process #include_aztec_version directives
 * These are version placeholders that should be replaced with actual version numbers
 */
export async function processIncludeVersionDirectives(
  content: string,
  repoPath: string,
): Promise<string> {
  // Derive commitTag from training.json
  const commitTag = commitTagFromTrainingConfig();

  // Fetch latest release dynamically for testnet tag
  const latestRelease = await getLatestAztecRelease();
  const testnetTag = latestRelease;

  // Replace aztec version placeholder with commit tag
  content = content.replaceAll(`#include_aztec_version`, commitTag);

  // Replace version without prefix (drop leading 'v' if present, otherwise use 'latest')
  content = content.replaceAll(
    `#include_version_without_prefix`,
    commitTag.startsWith("v") ? commitTag.substring(1) : "latest",
  );

  // Replace testnet version (drop leading 'v' if present)
  content = content.replaceAll(
    `#include_testnet_version`,
    testnetTag.startsWith("v") ? testnetTag.substring(1) : testnetTag,
  );

  return content;
}

/**
 * Main function to process all include directives in markdown content
 */
export async function preprocessMarkdownIncludes(
  markdownContent: string,
  repoPath: string,
): Promise<string> {
  let processedContent = markdownContent;

  // Process code inclusions
  processedContent = await processIncludeCodeDirectives(
    processedContent,
    repoPath,
  );

  // Process version inclusions
  processedContent = await processIncludeVersionDirectives(
    processedContent,
    repoPath,
  );

  return processedContent;
}
