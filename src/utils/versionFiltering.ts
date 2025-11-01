import { env } from "../env.js";
import { SimilaritySearchResponse } from "../types/data.js";

export interface VersionConfig {
  currentVersion: string;
  boostFactor: number;      // Default: 1.5
  penaltyFactor: number;    // Default: 0.5
}

/**
 * Filter and boost search results based on version matching
 * Prioritizes documents from the current version and deprioritizes outdated versions
 */
export function filterAndBoostByVersion(
  results: SimilaritySearchResponse,
  config: Partial<VersionConfig> = {}
): SimilaritySearchResponse {
  const currentVersion = config.currentVersion || env.AZTEC_DOCS_VERSION;

  if (!currentVersion) {
    // No version filtering if current version not set
    return results;
  }

  const boostFactor = config.boostFactor || 1.5;
  const penaltyFactor = config.penaltyFactor || 0.5;

  // Clone the results to avoid mutating the original
  const filteredResults: SimilaritySearchResponse = {
    ids: [...results.ids],
    embeddings: results.embeddings ? [...results.embeddings] : [],
    documents: [...results.documents],
    metadatas: results.metadatas ? [
      results.metadatas[0] ? [...results.metadatas[0]] : []
    ] : [[]],
    distances: results.distances ? [[...(results.distances[0] || [])]] : [[]],
  };

  // Adjust scores based on version match
  for (let idx = 0; idx < filteredResults.documents.length; idx++) {
    const metadata = filteredResults.metadatas[0]?.[idx];
    const docVersion = metadata?.versionTag as string | undefined;

    if (!docVersion) {
      // No version info, leave as-is
      continue;
    }

    let scoreMultiplier = 1.0;

    if (docVersion === currentVersion) {
      // Boost current version
      scoreMultiplier = boostFactor;
    } else if (isOutdatedVersion(docVersion, currentVersion)) {
      // Penalize old versions
      scoreMultiplier = penaltyFactor;
    }

    // Apply multiplier to distance (lower distance = better match)
    // Dividing by multiplier: boost reduces distance, penalty increases it
    if (filteredResults.distances?.[0]?.[idx] !== undefined) {
      filteredResults.distances[0][idx] = filteredResults.distances[0][idx] / scoreMultiplier;
    }
  }

  // Re-sort by adjusted distances
  return sortByDistance(filteredResults);
}

/**
 * Check if a document version is outdated compared to the current version
 * Handles semantic versioning and tags like "master", "main", "v1.2.3"
 */
function isOutdatedVersion(docVersion: string, currentVersion: string): boolean {
  // Normalize versions
  const normalizeVersion = (v: string): string => {
    return v.replace(/^v/, '').toLowerCase().trim();
  };

  const normDocVersion = normalizeVersion(docVersion);
  const normCurrentVersion = normalizeVersion(currentVersion);

  // If versions are identical after normalization, not outdated
  if (normDocVersion === normCurrentVersion) {
    return false;
  }

  // Branch names like "master" or "main" are considered current
  if (['master', 'main'].includes(normDocVersion)) {
    return false;
  }

  // Try semantic version comparison
  const docParts = parseSemanticVersion(normDocVersion);
  const currentParts = parseSemanticVersion(normCurrentVersion);

  if (docParts && currentParts) {
    // Compare major.minor.patch
    if (docParts.major < currentParts.major) return true;
    if (docParts.major > currentParts.major) return false;

    if (docParts.minor < currentParts.minor) return true;
    if (docParts.minor > currentParts.minor) return false;

    if (docParts.patch < currentParts.patch) return true;
    return false;
  }

  // If we can't parse versions, assume different = outdated
  return true;
}

/**
 * Parse semantic version string into components
 * Returns null if not a valid semantic version
 */
function parseSemanticVersion(version: string): { major: number; minor: number; patch: number } | null {
  // Match patterns like "1.2.3", "1.2", or "1"
  const match = version.match(/^(\d+)(?:\.(\d+))?(?:\.(\d+))?/);

  if (!match) {
    return null;
  }

  return {
    major: parseInt(match[1], 10),
    minor: match[2] ? parseInt(match[2], 10) : 0,
    patch: match[3] ? parseInt(match[3], 10) : 0,
  };
}

/**
 * Sort search results by distance (ascending - lower is better)
 */
function sortByDistance(results: SimilaritySearchResponse): SimilaritySearchResponse {
  if (!results.distances?.[0] || results.distances[0].length === 0) {
    return results;
  }

  // Create array of indices with their distances
  const indexed = results.distances[0].map((dist, idx) => ({ dist, idx }));

  // Sort by distance
  indexed.sort((a, b) => a.dist - b.dist);

  // Reorder all arrays based on sorted indices
  const sorted: SimilaritySearchResponse = {
    ids: indexed.map(item => results.ids[item.idx]),
    embeddings: results.embeddings?.[0]
      ? [indexed.map(item => results.embeddings![0][item.idx])]
      : [],
    documents: indexed.map(item => results.documents[item.idx]),
    metadatas: results.metadatas?.[0]
      ? [indexed.map(item => results.metadatas![0][item.idx])]
      : [[]],
    distances: [indexed.map(item => item.dist)],
  };

  return sorted;
}

/**
 * Get the latest version from environment or a default
 */
export function getCurrentVersion(): string | undefined {
  return env.AZTEC_DOCS_VERSION;
}
