import { formatCode } from "../formatter.js";
import { Data, ParserOptions } from "../common.js";

export async function parseNoir(
  code: string,
  options?: ParserOptions,
): Promise<Data[]> {
  const lines = await parseNewLines(code);
  const blobs: Data[] = [];
  const imports = shouldIncludeImports(options) ? extractUses(lines) : "";

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Skip doc comments
    if (line.trim().startsWith("//!")) continue;

    // Check for mod declarations
    if (matchNoirMod(line)) {
      const data = parseNoirMod(lines, i, imports);
      if (data && data.length > 0) {
        blobs.push(...data);
        i = data[data.length - 1].end;
      }
      continue;
    }

    // Check for trait declarations
    if (matchNoirTrait(line)) {
      const data = parseNoirTrait(lines, i, imports);
      if (data) {
        blobs.push(data);
        i = data.end;
      }
      continue;
    }

    // Check for function declarations (including unconstrained)
    if (matchNoirFunction(line)) {
      const data = parseNoirFunction(lines, i, imports, options);
      if (data) {
        blobs.push(data);
        i = data.end;
      }
      continue;
    }

    // Check for struct declarations
    if (matchNoirStruct(line)) {
      const data = parseNoirStruct(lines, i, imports);
      if (data) {
        blobs.push(data);
        i = data.end;
      }
      continue;
    }

    // Check for enum declarations
    if (matchNoirEnum(line)) {
      const data = parseNoirEnum(lines, i, imports);
      if (data) {
        blobs.push(data);
        i = data.end;
      }
      continue;
    }

    // Check for impl blocks
    if (matchNoirImpl(line)) {
      const implData = parseNoirImpl(lines, i, imports);
      if (implData) {
        blobs.push(implData);

        // Parse methods within the impl block
        const methodBlobs = parseNoirImplMethods(
          lines,
          i,
          implData.end,
          imports,
        );
        if (methodBlobs && methodBlobs.length > 0) {
          blobs.push(...methodBlobs);
        }

        i = implData.end;
      }
      continue;
    }

    // Check for global constants
    if (matchNoirGlobal(line)) {
      const data = parseNoirGlobal(lines, i, imports);
      if (data) {
        blobs.push(data);
        i = data.end;
      }
      continue;
    }
  }

  return mergeNoirChunks(blobs);
}

function matchNoirFunction(line: string): boolean {
  // Match function declarations including unconstrained functions
  return /^\s*(pub\s+)?(unconstrained\s+)?(async\s+)?fn\s+\w+/.test(line);
}

function matchNoirStruct(line: string): boolean {
  return /^\s*(pub\s+)?struct\s+\w+/.test(line);
}

function matchNoirImpl(line: string): boolean {
  return /^\s*impl(?:\s+<[^>]+>)?\s+\w+/.test(line);
}

function matchNoirTrait(line: string): boolean {
  return /^\s*(pub\s+)?trait\s+\w+/.test(line);
}

function matchNoirEnum(line: string): boolean {
  return /^\s*(pub\s+)?enum\s+\w+/.test(line);
}

function matchNoirMod(line: string): boolean {
  // Match mod declarations with optional attributes
  return (
    /^\s*(pub\s+)?(mod)\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\{/.test(line) ||
    /^\s*#\[.*\]\s*$/.test(line)
  );
}

function matchNoirGlobal(line: string): boolean {
  // Match global constant declarations
  return /^\s*(pub\s+)?global\s+\w+/.test(line);
}

function extractUses(lines: string[]): string {
  return lines.filter((line) => line.trim().startsWith("use ")).join("\n");
}

/**
 * Helper to determine if comments should be included based on options
 */
function shouldIncludeComments(options?: ParserOptions): boolean {
  return options?.includeComments !== false;
}

/**
 * Helper to determine if imports should be included based on options
 */
function shouldIncludeImports(options?: ParserOptions): boolean {
  return options?.includeImports !== false;
}

function parseNoirFunction(
  lines: string[],
  startIndex: number,
  imports: string,
  options?: ParserOptions,
): Data {
  const docComments = shouldIncludeComments(options)
    ? captureDocComments(lines, startIndex)
    : [];
  const attributes = captureAttributes(lines, startIndex);
  let braceCount = 0;
  let parenCount = 0;
  let functionBody = "";
  let endIndex = startIndex;
  let started = false;
  let functionSignatureComplete = false;

  // Capture the function definition and body
  for (let i = startIndex; i < lines.length; i++) {
    const currentLine = lines[i];

    // Track parentheses to know when function signature is complete
    if (!functionSignatureComplete) {
      parenCount += (currentLine.match(/\(/g) || []).length;
      parenCount -= (currentLine.match(/\)/g) || []).length;

      // Function signature is complete when parens are balanced and we see either { or ->
      if (
        parenCount === 0 &&
        (currentLine.includes("{") || currentLine.includes("->"))
      ) {
        functionSignatureComplete = true;
      }
    }

    if (!started && currentLine.includes("{")) {
      started = true;
    }
    if (started) {
      braceCount += (currentLine.match(/{/g) || []).length;
      braceCount -= (currentLine.match(/}/g) || []).length;
    }
    functionBody += currentLine + "\n";
    if (started && braceCount === 0) {
      endIndex = i;
      break;
    }
  }

  let content = imports ? `${imports}\n` : "";

  // Add doc comments with /// prefix
  if (docComments.length > 0) {
    content += docComments.map((c) => `/// ${c}`).join("\n") + "\n";
  }

  // Add attributes
  if (attributes.length > 0) {
    content += attributes.join("\n") + "\n";
  }

  content += functionBody;

  // Extract function name from potentially multi-line signature
  let name = "anonymous";
  let isUnconstrained = false;

  // Look through the function signature lines to find the name
  for (
    let i = startIndex;
    i < lines.length && !functionSignatureComplete;
    i++
  ) {
    const line = lines[i];
    const nameMatch = line.match(/fn\s+(\w+)/);
    if (nameMatch) {
      name = nameMatch[1];
      break;
    }
  }

  // Check if function is unconstrained (could be on any line of the signature)
  for (
    let i = startIndex;
    i < lines.length && !functionSignatureComplete;
    i++
  ) {
    if (lines[i].includes("unconstrained")) {
      isUnconstrained = true;
      break;
    }
  }

  const prefix = isUnconstrained ? "unconstrained fn" : "fn";

  return {
    title: `${prefix}: ${name} (${startIndex}-${endIndex})`,
    content: content.trimEnd(),
    start: startIndex,
    end: endIndex,
  };
}

function parseNoirStruct(
  lines: string[],
  startIndex: number,
  imports: string,
): Data {
  const docComments = captureDocComments(lines, startIndex);
  let braceCount = 0;
  let structBody = "";
  let endIndex = startIndex;
  let started = false;

  for (let i = startIndex; i < lines.length; i++) {
    const currentLine = lines[i];
    if (!started && currentLine.includes("{")) {
      started = true;
    }
    if (started) {
      braceCount += (currentLine.match(/{/g) || []).length;
      braceCount -= (currentLine.match(/}/g) || []).length;
    }
    structBody += currentLine + "\n";
    if (started && braceCount === 0) {
      endIndex = i;
      break;
    }
  }

  let content = imports ? `${imports}\n` : "";
  if (docComments.length > 0) {
    content += docComments.map((c) => `/// ${c}`).join("\n") + "\n";
  }
  content += structBody;

  const nameMatch = lines[startIndex].match(/struct\s+(\w+)/);
  const name = nameMatch?.[1] || "anonymous";

  return {
    title: `struct: ${name} (${startIndex}-${endIndex})`,
    content: content.trimEnd(),
    start: startIndex,
    end: endIndex,
  };
}

function parseNoirTrait(
  lines: string[],
  startIndex: number,
  imports: string,
): Data {
  const docComments = captureDocComments(lines, startIndex);
  let braceCount = 0;
  let traitBody = "";
  let endIndex = startIndex;
  let started = false;

  for (let i = startIndex; i < lines.length; i++) {
    const currentLine = lines[i];
    if (!started && currentLine.includes("{")) {
      started = true;
    }
    if (started) {
      braceCount += (currentLine.match(/{/g) || []).length;
      braceCount -= (currentLine.match(/}/g) || []).length;
    }
    traitBody += currentLine + "\n";
    if (started && braceCount === 0) {
      endIndex = i;
      break;
    }
  }

  let content = imports ? `${imports}\n` : "";
  if (docComments.length > 0) {
    content += docComments.map((c) => `/// ${c}`).join("\n") + "\n";
  }
  content += traitBody;

  const nameMatch = lines[startIndex].match(/trait\s+(\w+)/);
  const name = nameMatch?.[1] || "anonymous";

  return {
    title: `trait: ${name} (${startIndex}-${endIndex})`,
    content: content.trimEnd(),
    start: startIndex,
    end: endIndex,
  };
}

function parseNoirEnum(
  lines: string[],
  startIndex: number,
  imports: string,
): Data {
  const docComments = captureDocComments(lines, startIndex);
  let braceCount = 0;
  let enumBody = "";
  let endIndex = startIndex;
  let started = false;

  for (let i = startIndex; i < lines.length; i++) {
    const currentLine = lines[i];
    if (!started && currentLine.includes("{")) {
      started = true;
    }
    if (started) {
      braceCount += (currentLine.match(/{/g) || []).length;
      braceCount -= (currentLine.match(/}/g) || []).length;
    }
    enumBody += currentLine + "\n";
    if (started && braceCount === 0) {
      endIndex = i;
      break;
    }
  }

  let content = imports ? `${imports}\n` : "";
  if (docComments.length > 0) {
    content += docComments.map((c) => `/// ${c}`).join("\n") + "\n";
  }
  content += enumBody;

  const nameMatch = lines[startIndex].match(/enum\s+(\w+)/);
  const name = nameMatch?.[1] || "anonymous";

  return {
    title: `enum: ${name} (${startIndex}-${endIndex})`,
    content: content.trimEnd(),
    start: startIndex,
    end: endIndex,
  };
}

function parseNoirImpl(
  lines: string[],
  startIndex: number,
  imports: string,
): Data {
  const docComments = captureDocComments(lines, startIndex);
  let braceCount = 0;
  let started = false;
  let implBlock = "";
  let endIndex = startIndex;

  for (let i = startIndex; i < lines.length; i++) {
    const currentLine = lines[i];
    if (!started && currentLine.includes("{")) {
      started = true;
    }
    if (started) {
      braceCount += (currentLine.match(/{/g) || []).length;
      braceCount -= (currentLine.match(/}/g) || []).length;
    }
    implBlock += currentLine + "\n";
    if (started && braceCount === 0) {
      endIndex = i;
      break;
    }
  }

  const headerLine = lines[startIndex].trim();
  const implTypeMatch = headerLine.match(
    /impl\s+(?:<[^>]+>\s+)?([A-Za-z0-9_]+)/,
  );
  const implType = implTypeMatch ? implTypeMatch[1] : "anonymous";

  let content = imports ? `${imports}\n` : "";
  if (docComments.length > 0) {
    content += docComments.map((c) => `/// ${c}`).join("\n") + "\n";
  }
  content += implBlock;

  return {
    title: `impl block: ${implType} (${startIndex}-${endIndex})`,
    content: content.trimEnd(),
    start: startIndex,
    end: endIndex,
  };
}

function parseNoirGlobal(
  lines: string[],
  startIndex: number,
  imports: string,
): Data {
  const docComments = captureDocComments(lines, startIndex);
  let endIndex = startIndex;
  let globalBody = lines[startIndex];

  // Global constants are typically single line, but handle multi-line expressions
  let parenCount = 0;
  let braceCount = 0;
  let bracketCount = 0;

  for (let i = startIndex; i < lines.length; i++) {
    const currentLine = lines[i];
    parenCount += (currentLine.match(/\(/g) || []).length;
    parenCount -= (currentLine.match(/\)/g) || []).length;
    braceCount += (currentLine.match(/{/g) || []).length;
    braceCount -= (currentLine.match(/}/g) || []).length;
    bracketCount += (currentLine.match(/\[/g) || []).length;
    bracketCount -= (currentLine.match(/]/g) || []).length;

    if (i > startIndex) {
      globalBody += "\n" + currentLine;
    }

    // End when we reach a semicolon and all brackets are balanced
    if (
      currentLine.includes(";") &&
      parenCount === 0 &&
      braceCount === 0 &&
      bracketCount === 0
    ) {
      endIndex = i;
      break;
    }
  }

  let content = imports ? `${imports}\n` : "";
  if (docComments.length > 0) {
    content += docComments.map((c) => `/// ${c}`).join("\n") + "\n";
  }
  content += globalBody;

  const nameMatch = lines[startIndex].match(/global\s+(\w+)/);
  const name = nameMatch?.[1] || "anonymous";

  return {
    title: `global: ${name} (${startIndex}-${endIndex})`,
    content: content.trimEnd(),
    start: startIndex,
    end: endIndex,
  };
}

function parseNoirMod(
  lines: string[],
  startIndex: number,
  imports: string,
): Data[] {
  let actualStartIndex = startIndex;
  const attributes: string[] = [];

  // Collect attributes before mod declaration
  while (
    actualStartIndex < lines.length &&
    lines[actualStartIndex].trim().startsWith("#[")
  ) {
    attributes.push(lines[actualStartIndex]);
    actualStartIndex++;

    // Skip empty lines between attributes and mod
    while (
      actualStartIndex < lines.length &&
      lines[actualStartIndex].trim() === ""
    ) {
      actualStartIndex++;
    }
  }

  if (
    actualStartIndex >= lines.length ||
    !lines[actualStartIndex].trim().match(/^\s*(pub\s+)?(mod)\s+/)
  ) {
    return [];
  }

  const docComments = captureDocComments(lines, actualStartIndex);
  let braceCount = 0;
  let started = false;
  let modBlock = "";
  let endIndex = actualStartIndex;
  const allData: Data[] = [];

  // Include attributes in the mod block
  for (const attr of attributes) {
    modBlock += attr + "\n";
  }

  // Parse the mod block
  for (let i = actualStartIndex; i < lines.length; i++) {
    const currentLine = lines[i];
    if (!started && currentLine.includes("{")) {
      started = true;
    }
    if (started) {
      braceCount += (currentLine.match(/{/g) || []).length;
      braceCount -= (currentLine.match(/}/g) || []).length;
    }
    modBlock += currentLine + "\n";
    if (started && braceCount === 0) {
      endIndex = i;
      break;
    }
  }

  const headerLine = lines[actualStartIndex].trim();
  const modNameMatch = headerLine.match(/mod\s+([a-zA-Z_][a-zA-Z0-9_]*)/);
  const modName = modNameMatch ? modNameMatch[1] : "anonymous";

  const moduleImports = extractModuleImports(
    lines.slice(actualStartIndex + 1, endIndex),
  );
  const combinedImports = imports
    ? `${imports}\n${moduleImports}`
    : moduleImports;

  let content = combinedImports ? `${combinedImports}\n` : "";
  if (docComments.length > 0) {
    content += docComments.map((c) => `/// ${c}`).join("\n") + "\n";
  }
  content += modBlock;

  allData.push({
    title: `mod: ${modName} (${startIndex}-${endIndex})`,
    content: content,
    start: startIndex,
    end: endIndex,
  });

  // Parse functions within the module
  parseModuleFunctions(
    lines,
    actualStartIndex,
    endIndex,
    modName,
    combinedImports,
    allData,
  );

  return allData;
}

function parseNoirImplMethods(
  lines: string[],
  implStartIndex: number,
  implEndIndex: number,
  _imports: string,
): Data[] {
  const methodBlobs: Data[] = [];
  const implHeaderLine = lines[implStartIndex].trim();

  const implTypeMatch = implHeaderLine.match(
    /impl\s+(?:<[^>]+>\s+)?([A-Za-z0-9_]+)/,
  );
  const implType = implTypeMatch ? implTypeMatch[1] : "anonymous";

  let i = implStartIndex + 1;
  while (i < implEndIndex) {
    const line = lines[i].trim();

    // Skip only empty lines, but not comments - we need to capture them
    if (line === "") {
      i++;
      continue;
    }

    // Look for method definitions (including unconstrained methods)
    if (
      /^\s*(pub(\s*\([^)]*\))?\s+)?(unconstrained\s+)?(async\s+)?fn\s+\w+/.test(
        line,
      )
    ) {
      // Capture comments that come before the method (including multi-line comments above)
      const docComments = captureDocCommentsForMethod(lines, i);
      let methodBraceCount = 0;
      let methodParenCount = 0;
      const methodStartIndex = i;
      let methodEndIndex = i;
      let methodBody = "";
      let methodSignatureComplete = false;

      // Add captured doc comments
      if (docComments.length > 0) {
        methodBody += docComments.join("\n") + "\n";
      }

      let methodStarted = false;
      for (let j = i; j < implEndIndex; j++) {
        const currentLine = lines[j];
        methodBody += currentLine + "\n";

        // Track parentheses for multi-line method signatures
        if (!methodSignatureComplete) {
          methodParenCount += (currentLine.match(/\(/g) || []).length;
          methodParenCount -= (currentLine.match(/\)/g) || []).length;

          if (
            methodParenCount === 0 &&
            (currentLine.includes("{") || currentLine.includes("->"))
          ) {
            methodSignatureComplete = true;
          }
        }

        if (!methodStarted && currentLine.includes("{")) {
          methodStarted = true;
        }

        if (methodStarted) {
          methodBraceCount += (currentLine.match(/{/g) || []).length;
          methodBraceCount -= (currentLine.match(/}/g) || []).length;
        }

        if (methodStarted && methodBraceCount === 0) {
          methodEndIndex = j;
          i = j + 1;
          break;
        }
      }

      // Extract method name from potentially multi-line signature
      let methodName = "anonymous";
      let isUnconstrained = false;

      for (let j = methodStartIndex; j <= methodEndIndex; j++) {
        const sigLine = lines[j];
        const methodNameMatch = sigLine.match(/fn\s+(\w+)/);
        if (methodNameMatch) {
          methodName = methodNameMatch[1];
        }
        if (sigLine.includes("unconstrained")) {
          isUnconstrained = true;
        }
      }

      const methodPrefix = isUnconstrained ? "unconstrained fn" : "fn";
      const fullImports = extractCompleteUses(lines);

      methodBlobs.push({
        title: `impl ${implType} ${methodPrefix}: ${methodName} (${methodStartIndex}-${methodEndIndex})`,
        content: `${fullImports}\nimpl ${implType} {\n${methodBody}}`,
        start: methodStartIndex,
        end: methodEndIndex,
      });

      continue;
    }

    i++;
  }

  return methodBlobs;
}

function captureDocComments(lines: string[], fromIndex: number): string[] {
  const comments: string[] = [];
  let i = fromIndex - 1;

  while (
    i >= 0 &&
    (lines[i].trim().startsWith("///") ||
      lines[i].trim().startsWith("#[") ||
      lines[i].trim() === "")
  ) {
    const line = lines[i].trim();
    if (line.startsWith("///")) {
      comments.unshift(line.replace(/^\/\/\/\s?/, ""));
    }
    i--;
  }
  return comments;
}

/**
 * Specialized function to capture doc comments for methods within impl blocks
 * This captures both /// comments and // comments that precede the method
 */
function captureDocCommentsForMethod(
  lines: string[],
  fromIndex: number,
): string[] {
  const comments: string[] = [];
  let i = fromIndex - 1;

  // Look backwards for comments, but be more inclusive for impl methods
  while (i >= 0) {
    const line = lines[i].trim();

    // Stop if we hit another function or the impl block start
    if (
      line.match(/^\s*(pub\s+)?(unconstrained\s+)?fn\s+/) ||
      line.match(/^\s*impl\s+/) ||
      (line.includes("{") && !line.startsWith("//"))
    ) {
      break;
    }

    // Capture various comment types
    if (line.startsWith("///")) {
      comments.unshift(`    ${line}`);
    } else if (line.startsWith("//")) {
      comments.unshift(`    ${line}`);
    } else if (line.startsWith("#[")) {
      comments.unshift(`    ${line}`);
    } else if (line === "") {
      // Include empty lines in the comment block for proper formatting
      if (comments.length > 0) {
        comments.unshift("");
      }
    } else if (line.match(/^\s*(let\s+|const\s+)/)) {
      // Stop if we hit variable declarations (like `let N: u32 = 5;`)
      break;
    } else {
      // Stop if we hit other code
      break;
    }

    i--;
  }

  return comments;
}

function captureAttributes(lines: string[], fromIndex: number): string[] {
  const attributes: string[] = [];
  let i = fromIndex - 1;

  while (
    i >= 0 &&
    (lines[i].trim().startsWith("#[") || lines[i].trim() === "")
  ) {
    const line = lines[i].trim();
    if (line.startsWith("#[")) {
      attributes.unshift(line);
    }
    i--;
  }
  return attributes;
}

function extractCompleteUses(lines: string[]): string {
  const uses: string[] = [];
  let currentUse = "";
  let inMultilineUse = false;

  for (const line of lines) {
    const trimmedLine = line.trim();

    if (trimmedLine.startsWith("use ")) {
      currentUse = line;

      if (trimmedLine.endsWith(";")) {
        uses.push(currentUse);
        currentUse = "";
      } else {
        inMultilineUse = true;
      }
    } else if (inMultilineUse) {
      currentUse += "\n" + line;

      if (trimmedLine.endsWith(";")) {
        uses.push(currentUse);
        currentUse = "";
        inMultilineUse = false;
      }
    }
  }

  return uses.join("\n");
}

function extractModuleImports(moduleLines: string[]): string {
  const imports = [];
  for (let i = 0; i < moduleLines.length; i++) {
    const line = moduleLines[i].trim();
    if (line.startsWith("use ")) {
      imports.push(moduleLines[i]);
    }
  }
  return imports.join("\n");
}

function parseModuleFunctions(
  lines: string[],
  moduleStart: number,
  moduleEnd: number,
  moduleName: string,
  imports: string,
  results: Data[],
): void {
  let i = moduleStart + 1;

  while (i < moduleEnd) {
    const line = lines[i].trim();

    if (line === "" || line.startsWith("//")) {
      i++;
      continue;
    }

    // Look for test functions or other functions
    if (line.startsWith("#[test]") || matchNoirFunction(lines[i])) {
      const attributeLines = [];
      while (i < moduleEnd && lines[i].trim().startsWith("#[")) {
        attributeLines.push(lines[i]);
        i++;
      }

      if (i < moduleEnd && matchNoirFunction(lines[i])) {
        const func = parseNoirFunction(lines, i, imports);
        func.title = `mod ${moduleName} ${func.title}`;

        if (attributeLines.length > 0) {
          func.content = attributeLines.join("\n") + "\n" + func.content;
        }

        results.push(func);
        i = func.end + 1;
      } else {
        i++;
      }
    } else {
      i++;
    }
  }
}

/**
 * Merge small Noir chunks intelligently to avoid fragmentation
 * Groups by category and merges related small chunks together
 */
function mergeNoirChunks(chunks: Data[]): Data[] {
  const MIN_CHUNK_SIZE = 500; // Minimum characters for Noir chunks
  const MAX_MERGED_SIZE = 2000; // Maximum size for merged chunks

  // Separate large chunks that shouldn't be merged
  const largeFunctions = chunks.filter(
    (c) =>
      (c.title.includes("fn:") || c.title.includes("unconstrained fn:")) &&
      stripNoirImports(c.content).length >= MIN_CHUNK_SIZE,
  );

  const largeStructsTraits = chunks.filter(
    (c) =>
      (c.title.includes("struct:") ||
        c.title.includes("trait:") ||
        c.title.includes("impl block:")) &&
      stripNoirImports(c.content).length >= MIN_CHUNK_SIZE,
  );

  const largeMods = chunks.filter(
    (c) =>
      c.title.includes("mod:") &&
      stripNoirImports(c.content).length >= MIN_CHUNK_SIZE,
  );

  // Group small chunks by category
  const smallChunks = chunks.filter(
    (c) =>
      stripNoirImports(c.content).length < MIN_CHUNK_SIZE &&
      !largeFunctions.includes(c) &&
      !largeStructsTraits.includes(c) &&
      !largeMods.includes(c),
  );

  const categories = {
    globals: smallChunks.filter((c) => c.title.includes("global:")),
    enums: smallChunks.filter((c) => c.title.includes("enum:")),
    structs: smallChunks.filter((c) => c.title.includes("struct:")),
    traits: smallChunks.filter((c) => c.title.includes("trait:")),
    functions: smallChunks.filter(
      (c) => c.title.includes("fn:") || c.title.includes("unconstrained fn:"),
    ),
    implMethods: smallChunks.filter((c) => c.title.includes("impl ")),
    other: smallChunks.filter(
      (c) =>
        !c.title.includes("global:") &&
        !c.title.includes("enum:") &&
        !c.title.includes("struct:") &&
        !c.title.includes("trait:") &&
        !c.title.includes("fn:") &&
        !c.title.includes("unconstrained fn:") &&
        !c.title.includes("impl "),
    ),
  };

  const merged: Data[] = [
    ...largeFunctions,
    ...largeStructsTraits,
    ...largeMods,
  ];

  // Merge each category
  for (const [categoryName, categoryChunks] of Object.entries(categories)) {
    if (categoryChunks.length === 0) continue;

    // For impl methods, group by impl type
    if (categoryName === "implMethods") {
      const implsByType = new Map<string, Data[]>();

      for (const chunk of categoryChunks) {
        const implMatch = chunk.title.match(/impl (\w+)/);
        const implType = implMatch?.[1] || "unknown";

        if (!implsByType.has(implType)) {
          implsByType.set(implType, []);
        }
        implsByType.get(implType)!.push(chunk);
      }

      // Merge methods within each impl
      for (const [implType, implChunks] of implsByType) {
        merged.push(
          ...mergeNoirCategoryChunks(
            implChunks,
            `${implType} Methods`,
            MAX_MERGED_SIZE,
          ),
        );
      }
    } else {
      // Merge other categories normally
      merged.push(
        ...mergeNoirCategoryChunks(
          categoryChunks,
          categoryName,
          MAX_MERGED_SIZE,
        ),
      );
    }
  }

  // Sort by original position
  return merged.sort((a, b) => a.start - b.start);
}

/**
 * Merge chunks within a Noir category
 */
function mergeNoirCategoryChunks(
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
    const chunkSize = stripNoirImports(chunk.content).length;

    // If adding this chunk would exceed max size, finalize current group
    if (currentGroup.length > 0 && currentSize + chunkSize > maxSize) {
      if (currentGroup.length === 1) {
        merged.push(currentGroup[0]);
      } else {
        merged.push(createNoirMergedChunk(currentGroup, categoryName));
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
      merged.push(createNoirMergedChunk(currentGroup, categoryName));
    }
  }

  return merged;
}

/**
 * Create a merged chunk from multiple Noir chunks
 */
function createNoirMergedChunk(chunks: Data[], categoryName: string): Data {
  const uses = extractNoirUses(chunks[0].content);
  const contents = chunks
    .map((c) => stripNoirImports(c.content))
    .filter((content) => content.trim())
    .join("\n\n// ---\n\n");

  const names = chunks.map((c) => {
    // Extract name from titles like "fn: myFunction" or "struct: MyStruct"
    const match = c.title.match(
      /(?:fn|unconstrained fn|struct|trait|enum|global|impl \w+.*): (\w+)/,
    );
    return match?.[1] || "unknown";
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
    content: uses + (uses ? "\n\n" : "") + contents,
    start: chunks[0].start,
    end: chunks[chunks.length - 1].end,
  };
}

/**
 * Extract use statements from Noir code content
 */
function extractNoirUses(content: string): string {
  const lines = content.split("\n");
  return lines.filter((line) => line.trim().startsWith("use ")).join("\n");
}

/**
 * Remove use statements from Noir content for size calculation
 */
function stripNoirImports(content: string): string {
  const lines = content.split("\n");
  const nonUseLines = lines.filter((line) => !line.trim().startsWith("use "));
  return nonUseLines.join("\n");
}

async function parseNewLines(code: string): Promise<string[]> {
  if (typeof code !== "string") throw new Error("Input must be a string");

  // For Noir, we don't format the code - just parse it as-is
  const formattedCode = await formatCode(code, "noir");
  return formattedCode.split(/\r?\n/);
}
