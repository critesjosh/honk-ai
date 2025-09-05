import { spawn } from "child_process";
import prettier from "prettier";

// The parser needs the code to be line by line (No multi-line strings, arguments, etc)
// This was done to keep the parser simple and easy to understand VS implementing some syntax tree analysis

/**
 * Formats code using the appropriate formatter based on the parser type.
 *
 * @param code - The source code string to format
 * @param parser - The parser/language type (e.g., "rust", "typescript", "noir")
 * @returns A promise that resolves to the formatted code string
 */
export async function formatCode(
  code: string,
  parser: string,
): Promise<string> {
  try {
    switch (parser) {
      case "rust":
      case "rs":
        return await formatRustCode(code);
      case "noir":
      case "nr":
        return await formatNoirCode(code);
      default:
        return await prettier.format(code, {
          parser,
          printWidth: Infinity,
          semi: true,
        });
    }
  } catch (error) {
    console.error(`Error formatting code with parser ${parser}:`, error);
    console.log(code);
    console.log(parser);
    return code; // Return original code if formatting fails
  }
}

async function formatRustCode(code: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const rustCode = `#![cfg_attr(rust_analyzer, edition = "2021")]
${code}`;

    // Set up arguments for rustfmt
    const args = [
      "--edition",
      "2021", // Changed from 2024 to 2021
      "--config",
      "edition=2021,style_edition=2021,use_small_heuristics=Max,max_width=1000",
    ];

    const rustfmt = spawn("rustfmt", args);

    let stdout = "";
    let stderr = "";

    rustfmt.stdout.on("data", (data) => {
      stdout += data.toString();
    });

    rustfmt.stderr.on("data", (data) => {
      stderr += data.toString();
    });

    rustfmt.on("error", (error) => {
      reject(new Error(`Failed to start rustfmt: ${error.message}`));
    });

    rustfmt.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`rustfmt failed with code ${code}: ${stderr}`));
      } else {
        resolve(stdout);
      }
    });

    rustfmt.stdin.write(rustCode);
    rustfmt.stdin.end();
  });
}

// There is no current formatter for Noir, so we just return the code
// The parser does its best to parse the code when their is not standard formatting
async function formatNoirCode(code: string): Promise<string> {
  return code;
}
