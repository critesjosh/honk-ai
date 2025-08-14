import { returnAllDocuments, updateDocumentSource } from "../src/utils/chroma.js";

/**
 * Convert GitHub docs path to actual documentation website URL
 * Example: docs/docs/developers/getting_started.md -> https://docs.aztec.network/developers/getting_started
 */
function convertDocsPathToWebsiteUrl(githubPath: string): string {
  // Extract the relative path from the GitHub URL
  const match = githubPath.match(/\/docs\/docs\/(.+?)(?:#|$)/);
  if (!match) return githubPath;
  
  let docPath = match[1];
  
  // Remove .md/.mdx extension
  docPath = docPath.replace(/\.(md|mdx)$/, '');
  
  // Handle index files by removing the filename
  docPath = docPath.replace(/\/index$/, '');
  
  // Convert to docs website URL
  return `https://docs.aztec.network/${docPath}`;
}

/**
 * Update existing documents to use correct documentation URLs
 */
async function updateDocsUrls() {
  console.log("Starting docs URL update...");
  
  try {
    // Get all documents from ChromaDB
    console.log("Fetching all documents from ChromaDB...");
    const allDocuments = await returnAllDocuments();
    
    // Filter documents that are from aztec-packages docs
    const docsDocuments = allDocuments.filter(
      (doc) => doc.source.includes("https://github.com/AztecProtocol/aztec-packages/tree/master/docs/docs/")
    );
    
    if (docsDocuments.length === 0) {
      console.log("No docs documents found to update.");
      return;
    }
    
    console.log(`Found ${docsDocuments.length} docs documents to update.`);
    
    let updatedCount = 0;
    let failedCount = 0;
    
    for (const doc of docsDocuments) {
      try {
        // Convert GitHub URL to docs website URL
        const newUrl = convertDocsPathToWebsiteUrl(doc.source);
        
        if (newUrl === doc.source) {
          console.log(`Skipping ${doc.title} - no conversion needed`);
          continue;
        }
        
        console.log(`Updating: ${doc.source} -> ${newUrl}`);
        
        // Update just the source URL, preserving embeddings and content
        await updateDocumentSource(doc.title, newUrl);
        
        updatedCount++;
        
        // Small delay to be gentle on ChromaDB
        await new Promise((resolve) => setTimeout(resolve, 50));
        
      } catch (error) {
        console.error(`Failed to update document: ${doc.title}`, error);
        failedCount++;
      }
    }
    
    console.log(`\nUpdate complete:`);
    console.log(`- ${updatedCount} documents updated successfully`);
    console.log(`- ${failedCount} documents failed to update`);
    console.log(`- Total processed: ${docsDocuments.length}`);
    
  } catch (error) {
    console.error("Error during URL update:", error);
    process.exit(1);
  }
}

// Run the update
updateDocsUrls()
  .then(() => {
    console.log("Script completed successfully.");
    process.exit(0);
  })
  .catch((error) => {
    console.error("Script failed:", error);
    process.exit(1);
  });