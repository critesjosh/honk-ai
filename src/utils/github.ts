import { Octokit } from "@octokit/rest";
import { env } from "../env.js";

let cachedLatestRelease: { tag: string; fetchedAt: number } | null = null;
const CACHE_TTL = 5 * 60 * 1000; // 5 minutes

/**
 * Fetch the latest non-prerelease version of aztec-packages from GitHub
 * Results are cached for 5 minutes to avoid rate limiting
 */
export async function getLatestAztecRelease(): Promise<string> {
  // Return cached version if still valid
  if (cachedLatestRelease) {
    const age = Date.now() - cachedLatestRelease.fetchedAt;
    if (age < CACHE_TTL) {
      console.log(`Using cached latest release: ${cachedLatestRelease.tag}`);
      return cachedLatestRelease.tag;
    }
  }

  try {
    const octokit = new Octokit({
      auth: env.GITHUB_TOKEN,
    });

    console.log("Fetching latest aztec-packages release from GitHub...");

    const { data } = await octokit.repos.getLatestRelease({
      owner: "AztecProtocol",
      repo: "aztec-packages",
    });

    // getLatestRelease already filters out prereleases
    const tag = data.tag_name;
    console.log(`Latest aztec-packages release: ${tag}`);

    // Update cache
    cachedLatestRelease = {
      tag,
      fetchedAt: Date.now(),
    };

    return tag;
  } catch (error) {
    console.error("Failed to fetch latest aztec-packages release:", error);

    // If we have a stale cache, use it as fallback
    if (cachedLatestRelease) {
      console.warn(
        `Using stale cached release as fallback: ${cachedLatestRelease.tag}`,
      );
      return cachedLatestRelease.tag;
    }

    // Last resort: use a reasonable default
    console.warn(
      "No cached release available, using fallback: aztec-packages-v0.1.0",
    );
    return "aztec-packages-v0.1.0";
  }
}

/**
 * Clear the cached latest release (useful for testing or forcing a refresh)
 */
export function clearLatestReleaseCache(): void {
  cachedLatestRelease = null;
}
