import fs from "fs/promises";
import path from "path";
import os from "os";
import { preprocessMarkdownIncludes } from "../src/training/repos/preprocessing/codeInclusion.js";

async function main() {
  const tmpRoot = await fs.mkdtemp(
    path.join(os.tmpdir(), "honk-include-test-"),
  );
  const repoPath = tmpRoot;

  // Create a sample source file with docs markers
  const srcDir = path.join(repoPath, "src");
  await fs.mkdir(srcDir, { recursive: true });
  const sampleFile = path.join(srcDir, "foo.ts");
  const sampleContent = `// docs:start:hello
export function hello() {
  console.log("Hello world");
}
// docs:end:hello
`;
  await fs.writeFile(sampleFile, sampleContent, "utf-8");

  // Build sample markdown with version placeholders and include_code
  const md = `# Test Includes\n\nVersion tag: #include_aztec_version\nNo prefix version: #include_version_without_prefix\nTestnet tag: #include_testnet_version\n\n#include_code hello src/foo.ts typescript showLineNumbers`;

  const processed = await preprocessMarkdownIncludes(md, repoPath);

  console.log("===== Processed Markdown =====\n");
  console.log(processed);
  console.log("\n===== End =====");
}

main().catch((err) => {
  console.error("Test failed:", err);
  process.exit(1);
});
