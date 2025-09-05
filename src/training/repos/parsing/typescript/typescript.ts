import { formatCode } from "../formatter.js";
import { Data } from "../common.js";

/**
 * Main function that parses JavaScript or TypeScript code.
 * Finds function and class declarations, test blocks, and more,
 * returning them as an array of Data objects.
 */
export async function parseJS(code: string): Promise<Data[]> {
  const arrayOfLines = await parseNewLines(code);
  const blobs: Data[] = [];

  const imports = extractImports(arrayOfLines);

  // Loop through each line to detect function, class, or test blocks
  for (let i = 0; i < arrayOfLines.length; i++) {
    const line = arrayOfLines[i];

    // Function patterns (named, arrow, or object property)
    if (matchFunctionLine(line)) {
      const data = parseFunction(arrayOfLines, i, imports);
      if (data.content) {
        blobs.push(data);
      }
      i = data.end;
    }
    // Class pattern
    else if (
      /^\s*(?:export\s+(?:default\s+)?)?class\s+[a-zA-Z_][a-zA-Z0-9_]*/.test(
        line,
      )
    ) {
      const result = parseClass(arrayOfLines, i, imports);
      blobs.push(...result.blobs);
      i = result.end;
    }
    // describe(...) test block
    else if (/^\s*describe\s*\(/.test(line)) {
      const result = parseTests(arrayOfLines, i);
      blobs.push(...result.blobs);
      i = result.end;
    }
    // interface declarations
    else if (
      /^\s*(?:export\s+(?:default\s+)?)?interface\s+[a-zA-Z_][a-zA-Z0-9_]*/.test(
        line,
      )
    ) {
      const data = parseInterface(arrayOfLines, i, imports);
      if (data) {
        blobs.push(data);
        i = data.end;
      }
    }
    // type declarations
    else if (
      /^\s*(?:export\s+(?:default\s+)?)?type\s+[a-zA-Z_][a-zA-Z0-9_]*/.test(
        line,
      )
    ) {
      const data = parseTypeAlias(arrayOfLines, i, imports);
      if (data) {
        blobs.push(data);
        i = data.end;
      }
    }
    // enum declarations
    else if (
      /^\s*(?:export\s+(?:default\s+)?)?enum\s+[a-zA-Z_][a-zA-Z0-9_]*/.test(
        line,
      )
    ) {
      const data = parseEnum(arrayOfLines, i, imports);
      if (data) {
        blobs.push(data);
        i = data.end;
      }
    }
  }

  return mergeCodeChunks(blobs);
}

/**
 * Helper to detect if a line matches function patterns
 */
function matchFunctionLine(line: string): boolean {
  const functionPattern = new RegExp(
    [
      // Named function: function doStuff<T>(...)
      "^\\s*(?:export\\s+(?:default\\s+)?)?(?:async\\s+)?function\\s+",
      "([a-zA-Z_][a-zA-Z0-9_]*)",
      "(<[^>]+>)?",
      "\\s*\\([^)]*\\)",
      "|",
      // Arrow function: const doStuff<T> = (...) =>
      "^\\s*(?:export\\s+(?:default\\s+)?)?(?:const|let|var)\\s+",
      "([a-zA-Z_][a-zA-Z0-9_]*)",
      "(<[^>]+>)?",
      "\\s*=\\s*(?:async\\s*)?\\([^)]*\\)\\s*=>",
      "|",
      // Object property arrow function: doStuff<T>: (...) =>
      "^\\s*(?:export\\s+(?:default\\s+)?)?",
      "([a-zA-Z_][a-zA-Z0-9_]*)",
      "(<[^>]+>)?",
      "\\s*:\\s*(?:async\\s*)?\\([^)]*\\)\\s*=>",
    ].join(""),
  );

  return functionPattern.test(line);
}

/**
 * Extract import statements from code lines
 */
function extractImports(arrayOfLines: string[]): string {
  const imports: string[] = [];
  for (const line of arrayOfLines) {
    const trimmed = line.trim();
    if (trimmed.startsWith("import")) {
      imports.push(line);
    }
  }
  return imports.join("\n");
}

/**
 * Parse a function starting at index
 */
function parseFunction(
  arrayOfLines: string[],
  index: number,
  imports: string,
): Data {
  let braceCount = 0;
  const docComments = captureDocComments(arrayOfLines, index - 1);

  const functionPattern = new RegExp(
    [
      // Named function
      "^\\s*(?:export\\s+(?:default\\s+)?)?(?:async\\s+)?function\\s+",
      "([a-zA-Z_][a-zA-Z0-9_]*)",
      "(<[^>]+>)?",
      "\\s*\\([^)]*\\)",
      "|",
      // Arrow function (var/const/let)
      "^\\s*(?:export\\s+(?:default\\s+)?)?(?:const|let|var)\\s+",
      "([a-zA-Z_][a-zA-Z0-9_]*)",
      "(<[^>]+>)?",
      "\\s*=\\s*(?:async\\s*)?\\([^)]*\\)\\s*=>",
      "|",
      // Object property arrow function
      "^\\s*(?:export\\s+(?:default\\s+)?)?",
      "([a-zA-Z_][a-zA-Z0-9_]*)",
      "(<[^>]+>)?",
      "\\s*:\\s*(?:async\\s*)?\\([^)]*\\)\\s*=>",
    ].join(""),
  );

  const line = arrayOfLines[index];
  const match = line.match(functionPattern);
  if (!match) {
    return { title: "", content: "", start: index, end: index };
  }

  const functionName = match[1] || match[3] || match[5] || "anonymous";
  let functionBody = "";
  let end = index;

  // Add doc comments if found
  if (docComments.length > 0) {
    functionBody = docComments.map((comment) => `/** ${comment} */\n`).join("");
  }

  // Gather the function block up to matching braces
  for (let i = index; i < arrayOfLines.length; i++) {
    const currentLine = arrayOfLines[i];
    braceCount += (currentLine.match(/{/g) || []).length;
    braceCount -= (currentLine.match(/}/g) || []).length;

    functionBody += currentLine + "\n";

    if (braceCount === 0) {
      end = i;
      break;
    }
  }

  return {
    title: `Function: ${functionName} (${index}-${end})`,
    content: (imports + (imports ? "\n" : "") + functionBody).trimEnd(),
    start: index,
    end,
  };
}

/**
 * Parse a class declaration
 */
function parseClass(
  arrayOfLines: string[],
  index: number,
  imports: string,
): { end: number; blobs: Data[] } {
  let braceCount = 0;
  const classDocComments = captureDocComments(arrayOfLines, index - 1);
  const blobs: Data[] = [];

  const className =
    arrayOfLines[index].match(
      /^\s*(?:export\s+(?:default\s+)?)?class\s+([a-zA-Z_][a-zA-Z0-9_]*)/,
    )?.[1] || "anonymous";

  let end = index;
  const topLevelPlaceholder =
    classDocComments.map((c) => `/** ${c} */\n`).join("") +
    `class ${className} {\n`;

  for (let i = index; i < arrayOfLines.length; i++) {
    const line = arrayOfLines[i];

    if (line.includes("{")) braceCount++;
    if (line.includes("}")) braceCount--;

    if (braceCount === 0) {
      end = i;
      break;
    }

    const docComments = captureDocComments(arrayOfLines, i - 1);
    let snippetBody = docComments.map((c) => `/** ${c} */\n`).join("");

    // Constructor
    if (/^\s*constructor\s*\([^)]*\)\s*\{/.test(line)) {
      const ctorData = parseConstructor(arrayOfLines, i);
      ctorData.content =
        imports +
        (imports ? "\n" : "") +
        topLevelPlaceholder +
        ctorData.content +
        "\n}";
      ctorData.title = `Class: ${className} ${ctorData.title}`;
      blobs.push(ctorData);
      i = ctorData.end;
      continue;
    }

    // Method
    const methodPattern =
      /^\s*(?!if|for|while|switch|catch)(?:public|private|protected|static|async|\s)*[a-zA-Z_<][a-zA-Z0-9_<>]*\s*\([^)]*\)\s*\{/;
    if (methodPattern.test(line)) {
      let methodBraceCount = 0;
      let methodEnd = i;
      snippetBody += line + "\n";
      methodBraceCount += (line.match(/{/g) || []).length;
      methodBraceCount -= (line.match(/}/g) || []).length;

      for (let j = i + 1; j < arrayOfLines.length; j++) {
        const currentLine = arrayOfLines[j];
        snippetBody += currentLine + "\n";
        methodBraceCount += (currentLine.match(/{/g) || []).length;
        methodBraceCount -= (currentLine.match(/}/g) || []).length;

        if (methodBraceCount === 0) {
          methodEnd = j;
          i = j;
          break;
        }
      }

      const nameMatch = line.match(/\s+([a-zA-Z0-9_]+)\s*\([^)]*\)\s*\{/);
      const methodName = nameMatch?.[1] || "anonymousMethod";

      blobs.push({
        title: `Class: ${className} Method: ${methodName} (${i}-${methodEnd})`,
        content:
          imports +
          (imports ? "\n" : "") +
          topLevelPlaceholder +
          snippetBody +
          "\n}",
        start: i,
        end: methodEnd,
      });
      continue;
    }

    // Property
    const propertyPattern =
      /^\s*(?:public|private|protected|static|readonly)?\s*[a-zA-Z_][a-zA-Z0-9_]*(?:\s*:\s*[^=]+)?(\s*=\s*[^;]+)?;/;
    if (propertyPattern.test(line)) {
      const nameMatch = line.match(/\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(?::|=|;)/);
      const propName = nameMatch?.[1] || "anonymousProp";

      blobs.push({
        title: `Class: ${className} Property: ${propName} (${i}-${i})`,
        content:
          imports +
          (imports ? "\n" : "") +
          topLevelPlaceholder +
          snippetBody +
          line +
          "\n}",
        start: i,
        end: i,
      });
    }
  }

  return { end, blobs };
}

/**
 * Parse test blocks (describe/it)
 */
function parseTests(
  arrayOfLines: string[],
  index: number,
): { end: number; blobs: Data[] } {
  let braceCount = 0;
  const describeDocComments = captureDocComments(arrayOfLines, index - 1);
  const blobs: Data[] = [];

  const describeName =
    arrayOfLines[index].match(/describe\s*\(\s*(['"`])(.*?)\1/)?.[2] ||
    "anonymous";

  let end = index;
  const topLevelPlaceholder =
    describeDocComments.map((c) => `/** ${c} */\n`).join("") +
    `describe("${describeName}", () => {\n`;

  if (arrayOfLines[index].includes("{")) {
    braceCount++;
  }

  for (let i = index + 1; i < arrayOfLines.length; i++) {
    const line = arrayOfLines[i];

    if (line.includes("{")) braceCount++;
    if (line.includes("}")) braceCount--;

    if (braceCount === 0) {
      end = i;
      break;
    }

    const docComments = captureDocComments(arrayOfLines, i - 1);
    const testMatch = line.match(
      /^\s*(it|test)\s*\(\s*(['"`])(.*?)\2\s*,\s*\(\s*\)\s*=>\s*\{/,
    );

    if (testMatch) {
      let testBraceCount = 1;
      let testBody =
        docComments.map((c) => `/** ${c} */\n`).join("") + line + "\n";
      const testStart = i;
      let testEnd = i;

      for (let j = i + 1; j < arrayOfLines.length; j++) {
        const currentLine = arrayOfLines[j];
        testBody += currentLine + "\n";

        if (currentLine.includes("{")) testBraceCount++;
        if (currentLine.includes("}")) testBraceCount--;

        if (testBraceCount === 0) {
          testEnd = j;
          i = j;
          break;
        }
      }

      const testTitle = testMatch[3] || "anonymous_test";
      blobs.push({
        title: `Test: ${testTitle} (${testStart}-${testEnd})`,
        content: topLevelPlaceholder + testBody + "\n});",
        start: testStart,
        end: testEnd,
      });
      continue;
    }
  }

  return { end, blobs };
}

/**
 * Parse interface declarations
 */
function parseInterface(
  arrayOfLines: string[],
  index: number,
  imports: string,
): Data | null {
  const docComments = captureDocComments(arrayOfLines, index - 1);
  let braceCount = 0;
  let interfaceBody = "";
  let end = index;
  let started = false;

  for (let i = index; i < arrayOfLines.length; i++) {
    const currentLine = arrayOfLines[i];

    if (!started && currentLine.includes("{")) {
      started = true;
    }

    if (started) {
      braceCount += (currentLine.match(/{/g) || []).length;
      braceCount -= (currentLine.match(/}/g) || []).length;
    }

    interfaceBody += currentLine + "\n";

    if (started && braceCount === 0) {
      end = i;
      break;
    }
  }

  const nameMatch = arrayOfLines[index].match(
    /interface\s+([a-zA-Z_][a-zA-Z0-9_]*)/,
  );
  const name = nameMatch?.[1] || "anonymous";

  let content = imports ? `${imports}\n` : "";
  if (docComments.length > 0) {
    content += docComments.map((c) => `/** ${c} */\n`).join("");
  }
  content += interfaceBody;

  return {
    title: `Interface: ${name} (${index}-${end})`,
    content: content.trimEnd(),
    start: index,
    end,
  };
}

/**
 * Parse type alias declarations
 */
function parseTypeAlias(
  arrayOfLines: string[],
  index: number,
  imports: string,
): Data | null {
  const docComments = captureDocComments(arrayOfLines, index - 1);
  let end = index;
  let typeBody = arrayOfLines[index];

  // Type aliases can span multiple lines
  let parenCount = 0;
  let braceCount = 0;
  let angleCount = 0;

  for (let i = index; i < arrayOfLines.length; i++) {
    const currentLine = arrayOfLines[i];
    parenCount += (currentLine.match(/\(/g) || []).length;
    parenCount -= (currentLine.match(/\)/g) || []).length;
    braceCount += (currentLine.match(/{/g) || []).length;
    braceCount -= (currentLine.match(/}/g) || []).length;
    angleCount += (currentLine.match(/</g) || []).length;
    angleCount -= (currentLine.match(/>/g) || []).length;

    if (i > index) {
      typeBody += "\n" + currentLine;
    }

    if (
      currentLine.includes(";") &&
      parenCount === 0 &&
      braceCount === 0 &&
      angleCount === 0
    ) {
      end = i;
      break;
    }
  }

  const nameMatch = arrayOfLines[index].match(
    /type\s+([a-zA-Z_][a-zA-Z0-9_]*)/,
  );
  const name = nameMatch?.[1] || "anonymous";

  let content = imports ? `${imports}\n` : "";
  if (docComments.length > 0) {
    content += docComments.map((c) => `/** ${c} */\n`).join("");
  }
  content += typeBody;

  return {
    title: `Type: ${name} (${index}-${end})`,
    content: content.trimEnd(),
    start: index,
    end,
  };
}

/**
 * Parse enum declarations
 */
function parseEnum(
  arrayOfLines: string[],
  index: number,
  imports: string,
): Data | null {
  const docComments = captureDocComments(arrayOfLines, index - 1);
  let braceCount = 0;
  let enumBody = "";
  let end = index;
  let started = false;

  for (let i = index; i < arrayOfLines.length; i++) {
    const currentLine = arrayOfLines[i];

    if (!started && currentLine.includes("{")) {
      started = true;
    }

    if (started) {
      braceCount += (currentLine.match(/{/g) || []).length;
      braceCount -= (currentLine.match(/}/g) || []).length;
    }

    enumBody += currentLine + "\n";

    if (started && braceCount === 0) {
      end = i;
      break;
    }
  }

  const nameMatch = arrayOfLines[index].match(
    /enum\s+([a-zA-Z_][a-zA-Z0-9_]*)/,
  );
  const name = nameMatch?.[1] || "anonymous";

  let content = imports ? `${imports}\n` : "";
  if (docComments.length > 0) {
    content += docComments.map((c) => `/** ${c} */\n`).join("");
  }
  content += enumBody;

  return {
    title: `Enum: ${name} (${index}-${end})`,
    content: content.trimEnd(),
    start: index,
    end,
  };
}

/**
 * Parse constructor
 */
function parseConstructor(arrayOfLines: string[], index: number): Data {
  let braceCount = 0;
  const docComments = captureDocComments(arrayOfLines, index);

  const line = arrayOfLines[index];
  if (!/^\s*constructor\s*\([^)]*\)\s*\{/.test(line)) {
    return { title: "", content: "", start: index, end: index };
  }

  let body = "";
  let end = index;

  if (docComments.length > 0) {
    body += docComments.map((c) => `/** ${c} */\n`).join("");
  }

  for (let i = index; i < arrayOfLines.length; i++) {
    const currentLine = arrayOfLines[i];
    body += currentLine + "\n";

    braceCount += (currentLine.match(/{/g) || []).length;
    braceCount -= (currentLine.match(/}/g) || []).length;

    if (braceCount === 0) {
      end = i;
      break;
    }
  }

  return {
    title: `Constructor (${index}-${end})`,
    content: body.trimEnd(),
    start: index,
    end,
  };
}

/**
 * Capture doc comments above a line
 */
function captureDocComments(
  arrayOfLines: string[],
  fromIndex: number,
): string[] {
  const docBlocks: string[] = [];
  let currentBlock: string[] = [];
  let docIndex = fromIndex;

  const cleanLine = (line: string): string => {
    return line
      .replace(/^\/\*+|^\s*\*\/|\*\/$/g, "")
      .replace(/^\s*\*\s?/g, "")
      .replace(/^\/\/+/, "")
      .trim();
  };

  const commitBlock = () => {
    const block = currentBlock.join("\n").trim();
    if (block) {
      docBlocks.unshift(block);
      currentBlock = [];
    }
  };

  while (docIndex >= 0) {
    const line = arrayOfLines[docIndex].trim();

    if (!line) {
      docIndex--;
      continue;
    }

    if (line.includes("*/")) {
      while (docIndex >= 0) {
        const blockLine = arrayOfLines[docIndex].trim();
        const cleaned = cleanLine(blockLine);
        if (cleaned) currentBlock.unshift(cleaned);
        if (blockLine.startsWith("/*")) {
          commitBlock();
          break;
        }
        docIndex--;
      }
    } else if (line.startsWith("//")) {
      const cleaned = cleanLine(line);
      if (cleaned) currentBlock.unshift(cleaned);
    } else if (!line.startsWith("/*")) {
      commitBlock();
      break;
    }

    docIndex--;
  }

  commitBlock();
  return docBlocks;
}

/**
 * Merge small code chunks intelligently to avoid fragmentation
 * Groups by category and merges related small chunks together
 */
function mergeCodeChunks(chunks: Data[]): Data[] {
  const MIN_CODE_CHUNK_SIZE = 500; // Minimum characters for code chunks
  const MAX_MERGED_SIZE = 2000; // Maximum size for merged chunks

  // Separate large chunks that shouldn't be merged
  const largeFunctions = chunks.filter(
    (c) =>
      (c.title.includes("Function:") ||
        c.title.includes("Method:") ||
        c.title.includes("Constructor")) &&
      stripCodeImports(c.content).length >= MIN_CODE_CHUNK_SIZE,
  );

  const largeClasses = chunks.filter(
    (c) =>
      c.title.includes("Class:") &&
      !c.title.includes("Method:") &&
      !c.title.includes("Property:") &&
      stripCodeImports(c.content).length >= MIN_CODE_CHUNK_SIZE,
  );

  // Group small chunks by category for intelligent merging
  const smallChunks = chunks.filter(
    (c) =>
      stripCodeImports(c.content).length < MIN_CODE_CHUNK_SIZE &&
      !largeFunctions.includes(c) &&
      !largeClasses.includes(c),
  );

  // Group by category
  const categories = {
    types: smallChunks.filter(
      (c) =>
        c.title.includes("Type:") ||
        c.title.includes("Interface:") ||
        c.title.includes("Enum:"),
    ),
    properties: smallChunks.filter((c) => c.title.includes("Property:")),
    tests: smallChunks.filter((c) => c.title.includes("Test:")),
    functions: smallChunks.filter(
      (c) =>
        c.title.includes("Function:") ||
        c.title.includes("Method:") ||
        c.title.includes("Constructor"),
    ),
    other: smallChunks.filter(
      (c) =>
        !c.title.includes("Type:") &&
        !c.title.includes("Interface:") &&
        !c.title.includes("Enum:") &&
        !c.title.includes("Property:") &&
        !c.title.includes("Test:") &&
        !c.title.includes("Function:") &&
        !c.title.includes("Method:") &&
        !c.title.includes("Constructor"),
    ),
  };

  const merged: Data[] = [...largeFunctions, ...largeClasses];

  // Merge each category
  for (const [categoryName, categoryChunks] of Object.entries(categories)) {
    if (categoryChunks.length === 0) continue;

    // For properties, group by class
    if (categoryName === "properties") {
      const classesByName = new Map<string, Data[]>();

      for (const chunk of categoryChunks) {
        const classMatch = chunk.title.match(/Class: (\w+)/);
        const className = classMatch?.[1] || "unknown";

        if (!classesByName.has(className)) {
          classesByName.set(className, []);
        }
        classesByName.get(className)!.push(chunk);
      }

      // Merge properties within each class
      for (const [className, classChunks] of classesByName) {
        merged.push(
          ...mergeCategoryChunks(
            classChunks,
            `${className} Properties`,
            MAX_MERGED_SIZE,
          ),
        );
      }
    } else {
      // Merge other categories normally
      merged.push(
        ...mergeCategoryChunks(categoryChunks, categoryName, MAX_MERGED_SIZE),
      );
    }
  }

  // Sort by original position
  return merged.sort((a, b) => a.start - b.start);
}

/**
 * Merge chunks within a category
 */
function mergeCategoryChunks(
  chunks: Data[],
  categoryName: string,
  maxSize: number,
): Data[] {
  if (chunks.length <= 1) return chunks;

  const merged: Data[] = [];
  let currentGroup: Data[] = [];
  let currentSize = 0;

  // Sort by original position
  const sortedChunks = [...chunks].sort((a, b) => a.start - b.start);

  for (const chunk of sortedChunks) {
    const chunkSize = stripCodeImports(chunk.content).length;

    // If adding this chunk would exceed max size, finalize current group
    if (currentGroup.length > 0 && currentSize + chunkSize > maxSize) {
      if (currentGroup.length === 1) {
        merged.push(currentGroup[0]);
      } else {
        merged.push(createMergedChunk(currentGroup, categoryName));
      }
      currentGroup = [chunk];
      currentSize = chunkSize;
    } else {
      currentGroup.push(chunk);
      currentSize += chunkSize;
    }
  }

  // Handle remaining group
  if (currentGroup.length > 0) {
    if (currentGroup.length === 1) {
      merged.push(currentGroup[0]);
    } else {
      merged.push(createMergedChunk(currentGroup, categoryName));
    }
  }

  return merged;
}

/**
 * Create a merged chunk from multiple chunks
 */
function createMergedChunk(chunks: Data[], categoryName: string): Data {
  const imports = extractCodeImports(chunks[0].content);
  const contents = chunks
    .map((c) => stripCodeImports(c.content))
    .filter((content) => content.trim())
    .join("\n\n// ---\n\n");

  const names = chunks.map((c) => {
    const match = c.title.match(
      /(Type|Interface|Enum|Property|Function|Method|Constructor): (\w+)/,
    );
    return match?.[2] || "unknown";
  });

  const uniqueNames = [...new Set(names)];
  const title =
    uniqueNames.length > 3
      ? `Merged ${categoryName} (${uniqueNames.length} items)`
      : `Merged ${categoryName}: ${uniqueNames.join(", ")}`;

  console.log(
    `Merged ${chunks.length} ${categoryName} chunks: ${uniqueNames.join(", ")}`,
  );

  return {
    title: `${title} (${chunks[0].start}-${chunks[chunks.length - 1].end})`,
    content: imports + (imports ? "\n\n" : "") + contents,
    start: chunks[0].start,
    end: chunks[chunks.length - 1].end,
  };
}

/**
 * Extract imports from code content
 */
function extractCodeImports(content: string): string {
  const lines = content.split("\n");
  return lines.filter((line) => line.trim().startsWith("import")).join("\n");
}

/**
 * Remove imports from code content for size calculation
 */
function stripCodeImports(content: string): string {
  const lines = content.split("\n");
  const nonImportLines = lines.filter(
    (line) => !line.trim().startsWith("import"),
  );
  return nonImportLines.join("\n");
}

/**
 * Split code into lines with formatting
 */
async function parseNewLines(code: string): Promise<string[]> {
  if (typeof code !== "string") {
    throw new Error("Input must be a string");
  }

  const formattedCode = await formatCode(code, "typescript");
  const lines = formattedCode.split(/\r?\n/);
  return lines.map((line) => line.trimEnd());
}
