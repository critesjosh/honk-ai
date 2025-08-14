import { returnAllDocuments, deleteDocument } from "../src/utils/chroma.js";

/**
 * Delete all documents from aztec-packages repository
 */
async function deleteAztecPackagesDocuments() {
  console.log("Starting cleanup of aztec-packages documents...");
  
  try {
    // Get all documents from ChromaDB
    console.log("Fetching all documents from ChromaDB...");
    const allDocuments = await returnAllDocuments();
    
    // Filter documents that belong to aztec-packages repository
    const aztecPackagesDocuments = allDocuments.filter(
      (doc) => doc.source.includes("https://github.com/AztecProtocol/aztec-packages/")
    );
    
    if (aztecPackagesDocuments.length === 0) {
      console.log("No aztec-packages documents found to delete.");
      return;
    }
    
    console.log(`Found ${aztecPackagesDocuments.length} aztec-packages documents to delete.`);
    console.log("Deleting all documents...");
    
    let deletedCount = 0;
    let failedCount = 0;
    
    // Delete all documents at once
    const results = await Promise.allSettled(
      aztecPackagesDocuments.map((doc) => deleteDocument(doc.title))
    );
    
    // Count successes and failures
    results.forEach((result, index) => {
      if (result.status === "fulfilled" && result.value === true) {
        deletedCount++;
      } else {
        failedCount++;
        console.warn(`Failed to delete document: ${aztecPackagesDocuments[index].title}`);
        if (result.status === "rejected") {
          console.warn(`Error:`, result.reason);
        }
      }
    });
    
    console.log(`\nCleanup complete:`);
    console.log(`- ${deletedCount} documents deleted successfully`);
    console.log(`- ${failedCount} documents failed to delete`);
    console.log(`- Total processed: ${aztecPackagesDocuments.length}`);
    
  } catch (error) {
    console.error("Error during cleanup:", error);
    process.exit(1);
  }
}

// Run the cleanup
deleteAztecPackagesDocuments()
  .then(() => {
    console.log("Script completed successfully.");
    process.exit(0);
  })
  .catch((error) => {
    console.error("Script failed:", error);
    process.exit(1);
  });