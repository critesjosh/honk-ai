import { env } from "../src/env.js";

async function testGithubReleases() {
  try {
    console.log("Fetching releases from GitHub API...");
    const response = await fetch(
      "https://api.github.com/repos/AztecProtocol/aztec-packages/releases",
      {
        headers: {
          Authorization: `token ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github.v3+json",
        },
      },
    );

    if (!response.ok) {
      throw new Error(
        `GitHub API returned status ${response.status}: ${response.statusText}`,
      );
    }

    const releases = await response.json();

    console.log(`\nTotal releases found: ${releases.length}`);
    console.log("\nFirst 10 releases:");

    releases.slice(0, 10).forEach((release: any, index: number) => {
      console.log(`\n${index + 1}. ${release.tag_name}`);
      console.log(`   Name: ${release.name || "N/A"}`);
      console.log(`   Prerelease: ${release.prerelease}`);
      console.log(`   Draft: ${release.draft}`);
      console.log(`   Published: ${release.published_at}`);
    });

    // Find first non-prerelease
    const firstNonPrerelease = releases.find(
      (r: any) => !r.prerelease && !r.draft,
    );
    console.log("\n---");
    if (firstNonPrerelease) {
      console.log(
        `First non-prerelease: ${firstNonPrerelease.tag_name} (published: ${firstNonPrerelease.published_at})`,
      );
    } else {
      console.log("No non-prerelease found!");
      console.log(
        "Most recent release (may be prerelease):",
        releases[0]?.tag_name,
      );
    }
  } catch (error) {
    console.error("Error:", error);
  }
}

testGithubReleases();
