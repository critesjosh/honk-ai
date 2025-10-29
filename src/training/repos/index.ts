import { simpleGit, SimpleGit } from "simple-git";
import { glob } from "glob";
import config from "../../config/training.json" with { type: "json" };
import path from "path";
import { fileURLToPath } from "url";
import fs from "fs/promises";
import {
  UpsertDocument,
  returnAllDocuments,
  deleteDocument,
} from "../../utils/chroma.js";
import { parseNoir, parseJS, Data, chunkMdxFile } from "./parsing/index.js";
import { preprocessMarkdownIncludes } from "./preprocessing/codeInclusion.js";
import { env } from "../../env.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Main training service that processes repositories and trains the AI on code
 */
export async function StartTrainingService() {
  console.log("Starting repository training service");

  try {
    // Process repositories sequentially to avoid overwhelming the system
    for (const repo of config.repositories) {
      await processRepository(repo);
    }

    console.log("Repository training service completed");
  } catch (error) {
    console.error("Error in training service:", error);
    throw error;
  }
}

/**
 * Process a single repository
 */
async function processRepository(repo: any) {
  // Determine which version/branch to use
  let versionToCheckout = repo.branch || "main";

  // If useEnvVersion is true, use AZTEC_DOCS_VERSION from env or fetch latest release
  if (repo.useEnvVersion) {
    if (env.AZTEC_DOCS_VERSION) {
      versionToCheckout = env.AZTEC_DOCS_VERSION;
      console.log(`Using version from AZTEC_DOCS_VERSION: ${versionToCheckout}`);
    } else {
      // Fetch latest release if AZTEC_DOCS_VERSION not set
      const { getLatestAztecRelease } = await import("../../utils/github.js");
      versionToCheckout = await getLatestAztecRelease();
      console.log(`Using latest release: ${versionToCheckout}`);
    }
  }

  const repoUrl = `https://github.com/${repo.org}/${repo.name}/tree/${versionToCheckout}`;
  const repoPath = path.join(__dirname, "temp", repo.name);

  console.log(`Processing repository: ${repo.name} at version ${versionToCheckout}`);

  try {
    // Clean up any existing repository
    await cleanupRepository(repoPath);

    // Clone the repository
    const git: SimpleGit = simpleGit();
    console.log(`Cloning ${repo.org}/${repo.name}...`);
    await git.clone(
      `https://github.com/${repo.org}/${repo.name}.git`,
      repoPath,
    );

    // Checkout specified version (branch or tag)
    console.log(`Checking out ${versionToCheckout}...`);
    await git.cwd(repoPath).checkout(versionToCheckout);

    // Delete old documents for this repository
    await cleanupOldDocuments(repoUrl);

    // Process files according to patterns
    if (repo.patterns?.length > 0) {
      await processRepoFiles(repo, repoPath, repoUrl);
    } else {
      console.log(`No patterns defined for ${repo.name}, skipping`);
    }

    // Clean up cloned repository
    await cleanupRepository(repoPath);

    console.log(`Completed processing ${repo.name}`);
  } catch (error) {
    console.error(`Error processing repository ${repo.name}:`, error);
    await cleanupRepository(repoPath); // Ensure cleanup even on error
  }
}

/**
 * Process files in a repository according to defined patterns
 */
async function processRepoFiles(repo: any, repoPath: string, repoUrl: string) {
  const fileStats = { processed: 0, failed: 0 };

  for (const pattern of repo.patterns) {
    console.log(`Processing pattern: ${pattern}`);
    const files = await glob(pattern, { cwd: repoPath, absolute: true });
    console.log(`Found ${files.length} files matching pattern ${pattern}`);

    // Process files in batches to avoid memory issues
    const batchSize = 10;
    for (let i = 0; i < files.length; i += batchSize) {
      const batch = files.slice(i, i + batchSize);
      console.log(
        `Processing batch ${Math.floor(i / batchSize) + 1}/${Math.ceil(files.length / batchSize)} (${batch.length} files)`,
      );

      const results = await Promise.allSettled(
        batch.map((file) => processFile(file, repoPath, repoUrl, fileStats)),
      );

      // Log any rejected promises for debugging
      results.forEach((result, index) => {
        if (result.status === "rejected") {
          console.error(
            `Failed to process file ${batch[index]}:`,
            result.reason,
          );
        }
      });
    }
  }

  console.log(
    `File processing stats: ${fileStats.processed} processed, ${fileStats.failed} failed`,
  );
}

/**
 * Process a single file
 */
async function processFile(
  filePath: string,
  repoPath: string,
  repoUrl: string,
  stats: { processed: number; failed: number },
) {
  try {
    const relativePath = path.relative(repoPath, filePath);
    console.log(`Processing file: ${relativePath}`);
    let content = await fs.readFile(filePath, "utf-8");

    // Skip empty files or very large files (>1MB)
    if (!content.trim() || content.length > 1024 * 1024) {
      console.log(`Skipping ${relativePath} (empty or too large)`);
      return;
    }

    const isMarkdown =
      relativePath.endsWith(".md") ||
      relativePath.endsWith(".mdx") ||
      relativePath.endsWith(".markdown");

    // Preprocess markdown include directives prior to any storage
    if (isMarkdown) {
      try {
        content = await preprocessMarkdownIncludes(content, repoPath);
        console.log(`Preprocessed markdown includes for: ${relativePath}`);
      } catch (e) {
        console.warn(`Failed preprocessing includes for ${relativePath}:`, e);
      }

      // Store whole document only (pause chunking for markdown)
      const wholeDocTitle = `${repoUrl}/${relativePath} [FULL_DOCUMENT]`;
      let wholeDocUrl = `${repoUrl}/${relativePath}`;
      if (relativePath.startsWith("docs/docs/")) {
        wholeDocUrl = convertDocsPathToWebsiteUrl(relativePath);
      }
      const wholeDocContent = `${wholeDocTitle}\n\n${content}`;
      await UpsertDocument(wholeDocTitle, wholeDocContent, wholeDocUrl);
      console.log(`Stored full markdown document for ${relativePath}`);

      stats.processed++;
      return;
    }

    // If Noir/Rust file is small (<1500 tokens), store full file without chunking
    const isNoirLike =
      relativePath.endsWith(".nr") ||
      relativePath.endsWith(".noir") ||
      relativePath.endsWith(".rs");

    if (isNoirLike) {
      const estimatedTokens = estimateTokens(content);
      console.log(estimatedTokens);
      if (estimatedTokens < 1500) {
        const wholeFileTitle = `${repoUrl}/${relativePath} [FULL_FILE]`;
        const wholeFileUrl = `${repoUrl}/${relativePath}`;
        const wholeFileContent = `${wholeFileTitle}\n\n${content}`;
        await UpsertDocument(wholeFileTitle, wholeFileContent, wholeFileUrl);
        console.log(
          `Stored full Noir/Rust file (small size, ~${estimatedTokens} tokens): ${relativePath}`,
        );
        stats.processed++;
        return;
      }
    }

    const chunks = await parseCode(content, relativePath, repoPath);
    console.log(`Parsed ${chunks.length} chunks from ${relativePath}`);

    // Process chunks sequentially to avoid overwhelming ChromaDB
    for (const chunk of chunks) {
      const documentTitle = `${repoUrl}/${relativePath} ${chunk.title}`;

      // Convert docs paths to actual documentation website URLs
      let documentUrl = `${repoUrl}/${relativePath}#L${chunk.start}-L${chunk.end}`;
      if (relativePath.startsWith("docs/docs/")) {
        documentUrl = convertDocsPathToWebsiteUrl(relativePath);
      }

      const documentContent = `${documentTitle}\n\n${chunk.content}`;

      await UpsertDocument(documentTitle, documentContent, documentUrl);
    }

    stats.processed++;
  } catch (error) {
    console.warn(`Failed to process file ${filePath}:`, error);
    stats.failed++;
  }
}

/**
 * Parse code into chunks based on file type
 */
async function parseCode(
  content: string,
  filename: string,
  repoPath: string,
): Promise<Data[]> {
  const extension = getFileExtension(filename);

  switch (extension) {
    case "nr":
    case "noir":
    // Use noir parser for rust files since they are very similar
    case "rs":
      return await parseNoir(content);
    case "ts":
    case "tsx":
    case "js":
    case "jsx":
      return await parseJS(content);
    case "md":
    case "mdx":
    case "markdown":
      // Preprocess markdown to resolve #include_code directives
      const processedContent = await preprocessMarkdownIncludes(
        content,
        repoPath,
      );
      console.log(`Preprocessed markdown includes for: ${filename}`);

      // Use chunking for markdown to create proper sections
      return chunkMdxFile(processedContent);
    default:
      // Return the entire file as a single chunk for unsupported types
      return [
        {
          title: `File: ${filename}`,
          content: content,
          start: 1,
          end: content.split("\n").length,
        },
      ];
  }
}

/**
 * Get file extension from filename
 */
function getFileExtension(filename: string): string {
  const parts = filename.split(".");
  return parts.length > 1 ? parts[parts.length - 1].toLowerCase() : "";
}

/**
 * Check if file is a markdown file
 * Currently unused but kept for potential future use
 */
// function isMarkdownFile(filename: string): boolean {
//   const extension = getFileExtension(filename);
//   return ["md", "mdx", "markdown"].includes(extension);
// }

/**
 * Clean up repository directory
 */
async function cleanupRepository(repoPath: string): Promise<void> {
  try {
    const stats = await fs.stat(repoPath);
    if (stats.isDirectory()) {
      console.log(`Cleaning up ${repoPath}`);
      await fs.rm(repoPath, { recursive: true, force: true });
    }
  } catch {
    // Directory doesn't exist, which is fine
  }
}

/**
 * Clean up old documents for a repository by finding documents with matching repo URL
 * and deleting them from ChromaDB
 */
async function cleanupOldDocuments(repoUrl: string): Promise<void> {
  console.log(`Cleaning up old documents for ${repoUrl}`);

  try {
    // Get all documents from ChromaDB
    const allDocuments = await returnAllDocuments();

    // Filter documents that belong to this repository
    const repoDocuments = allDocuments.filter(
      (doc) => doc.source.includes(repoUrl) || doc.title.includes(repoUrl),
    );

    if (repoDocuments.length === 0) {
      console.log(`No existing documents found for ${repoUrl}`);
      return;
    }

    console.log(
      `Found ${repoDocuments.length} existing documents for ${repoUrl}, deleting...`,
    );

    // Delete documents in batches to avoid overwhelming ChromaDB
    const batchSize = 50;
    let deletedCount = 0;
    let failedCount = 0;

    for (let i = 0; i < repoDocuments.length; i += batchSize) {
      const batch = repoDocuments.slice(i, i + batchSize);

      // Process batch with Promise.allSettled to handle individual failures
      const results = await Promise.allSettled(
        batch.map((doc) => deleteDocument(doc.title)),
      );

      // Count successes and failures
      results.forEach((result, index) => {
        if (result.status === "fulfilled" && result.value === true) {
          deletedCount++;
        } else {
          failedCount++;
          console.warn(`Failed to delete document: ${batch[index].title}`);
        }
      });

      // Add small delay between batches to be gentle on ChromaDB
      if (i + batchSize < repoDocuments.length) {
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
    }

    console.log(
      `Cleanup complete for ${repoUrl}: ${deletedCount} deleted, ${failedCount} failed`,
    );
  } catch (error) {
    console.error(`Error cleaning up documents for ${repoUrl}:`, error);
    // Don't throw the error - continue with training even if cleanup fails
  }
}

/**
 * Convert GitHub docs path to actual documentation website URL
 * Example: docs/docs/developers/getting_started.md -> https://docs.aztec.network/developers/getting_started
 */
function convertDocsPathToWebsiteUrl(relativePath: string): string {
  // Remove docs/docs/ prefix and .md/.mdx extension
  let docPath = relativePath
    .replace(/^docs\/docs\//, "")
    .replace(/\.(md|mdx)$/, "");

  // Handle index files by removing the filename
  docPath = docPath.replace(/\/index$/, "");

  // Convert to docs website URL
  return `https://docs.aztec.network/${docPath}`;
}

function estimateTokens(text: string): number {
  // Simple heuristic: ~4 characters per token
  return Math.ceil(text.length / 4);
}
