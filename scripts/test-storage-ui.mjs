// Contract tests for the storage view model. As with the jobs/readiness
// tests, esbuild bundles the pure module and node:assert verifies the
// fixed-copy contract without a DOM test framework.
import assert from "node:assert/strict";
import { build } from "esbuild";

const result = await build({
  entryPoints: ["src/features/storage/storageModel.ts"],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false
});
const source = result.outputFiles[0].text;
const module = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const {
  cleanupConfirmCopy,
  cleanupPreviewCopy,
  cleanupResultCopy,
  deleteSourceConfirmCopy,
  deleteSourceErrorCopy,
  deleteSourceResultCopy,
  formatBytes,
  lowDiskCopy,
  skippedCopy,
  storageCategoryRows,
  storageErrorCopy
} = module;

let assertionCount = 0;
function equal(actual, expected, message) {
  assertionCount += 1;
  assert.equal(actual, expected, message);
}
function ok(value, message) {
  assertionCount += 1;
  assert.ok(value, message);
}

const PRIVATE_SENTINEL = "PRIVATE_UI_SENTINEL_MUST_NOT_APPEAR";

function status(overrides = {}) {
  return {
    profile_dir: "C:\\profiles\\default",
    total_bytes: 1536,
    categories: {
      database: { bytes: 512, files: 1 },
      documents: { bytes: 1024, files: 2 },
      images: { bytes: 0, files: 0 },
      logs: { bytes: 0, files: 0 },
      models: { bytes: 0, files: 0 },
      other: { bytes: 0, files: 0 }
    },
    skipped_count: 0,
    free_disk_bytes: 50 * 1024 * 1024 * 1024,
    low_disk: false,
    low_disk_threshold_bytes: 1024 * 1024 * 1024,
    source_counts: { documents: 2, images: 0 },
    ...overrides
  };
}

function preview(overrides = {}) {
  return {
    embedding_cache: { rows: 3, bytes: 3072 },
    orphan_files: { files: 1, bytes: 2048, skipped: 0 },
    legacy_deleted_sources: { documents: 0, artifacts: 0, rows: 0, files: 0, bytes: 0 },
    reclaimable_file_bytes: 2048,
    reclaimable_db_bytes: 3072,
    ...overrides
  };
}

// ---------------------------------------------------------------- formatBytes
equal(formatBytes(0), "0 B");
equal(formatBytes(1023), "1023 B");
equal(formatBytes(1024), "1.0 KB");
equal(formatBytes(1536), "1.5 KB");
equal(formatBytes(1024 * 1024), "1.0 MB");
equal(formatBytes(-1), "unknown", "negative bytes are unknown, never fabricated");
equal(formatBytes(Number.NaN), "unknown");

// ----------------------------------------------------------- category mapping
const rows = storageCategoryRows(status());
equal(rows.length, 6, "all six fixed categories always render");
equal(rows[0].key, "documents", "documents listed first");
ok(rows.every((row) => row.label && row.explanation), "every category has fixed copy");
const dbRow = rows.find((row) => row.key === "database");
ok(dbRow.explanation.includes("journal"), "database copy explains WAL/journal inclusion");
const modelsRow = rows.find((row) => row.key === "models");
ok(modelsRow.explanation.includes("Never touched"), "models copy states cleanup exclusion");

// Missing category buckets degrade to zero, never crash.
const sparse = storageCategoryRows(status({ categories: {} }));
equal(sparse.length, 6);
equal(sparse[0].bytes, 0);

// ------------------------------------------------------------------- low disk
ok(lowDiskCopy(status()).includes("free on this drive"));
const low = lowDiskCopy(status({ low_disk: true, free_disk_bytes: 512 * 1024 * 1024 }));
ok(low.startsWith("Low disk space"), "low-disk state leads with the warning");
ok(low.includes("512"), "low-disk copy carries the measured free bytes");
const unknown = lowDiskCopy(status({ free_disk_bytes: -1 }));
ok(unknown.includes("could not be measured"), "unmeasurable free space is honest");

// -------------------------------------------------------------------- skipped
equal(skippedCopy(0), null, "no skipped line when nothing was skipped");
ok(skippedCopy(1).includes("1 item"), "singular skipped copy");
ok(skippedCopy(3).includes("3 items"), "plural skipped copy");
ok(skippedCopy(2).includes("not included in the totals"), "skipped items are declared unmeasured");

// ------------------------------------------------------------ cleanup preview
equal(
  cleanupPreviewCopy(preview({ reclaimable_file_bytes: 0, reclaimable_db_bytes: 0 })),
  "Nothing to clean up right now."
);
const previewCopy = cleanupPreviewCopy(preview());
ok(previewCopy.includes("2.0 KB"), "file bytes surface in preview copy");
ok(previewCopy.includes("does not shrink"), "database honesty note always present when db bytes exist");
const confirm = cleanupConfirmCopy(preview());
ok(confirm.includes("Run cleanup?"));
ok(confirm.includes("are not touched"), "confirm copy states the exclusions");

// ------------------------------------------------------------- cleanup result
const resultCopy = cleanupResultCopy({
  embedding_cache: { rows: 3, bytes: 3072 },
  orphan_files: { files: 1, bytes: 2048, skipped: 1, failed: 2 },
  legacy_deleted_sources: { documents: 0, artifacts: 0, rows: 0, files: 0, bytes: 0, skipped: 0, failed: 0 },
  reclaimed_file_bytes: 2048,
  reclaimed_db_bytes: 3072,
  skipped_count: 1,
  failed_count: 2
});
ok(resultCopy.includes("2.0 KB of files removed"), "exact reclaimed bytes reported");
ok(resultCopy.includes("2 items could not be removed"), "failed count reported without paths");
ok(resultCopy.includes("1 item"), "skipped count reported");
const emptyResult = cleanupResultCopy({
  embedding_cache: { rows: 0, bytes: 0 },
  orphan_files: { files: 0, bytes: 0, skipped: 0, failed: 0 },
  legacy_deleted_sources: { documents: 0, artifacts: 0, rows: 0, files: 0, bytes: 0, skipped: 0, failed: 0 },
  reclaimed_file_bytes: 0,
  reclaimed_db_bytes: 0,
  skipped_count: 0,
  failed_count: 0
});
ok(emptyResult.includes("nothing needed cleaning"));

// ------------------------------------------------------------- source delete
const confirmCopy = deleteSourceConfirmCopy("Tax return 2025.pdf");
ok(confirmCopy.includes('Delete "Tax return 2025.pdf"?'));
ok(confirmCopy.includes("local copy"), "confirm explains the app copy is removed");
ok(confirmCopy.includes("not touched"), "confirm explains the original survives");

equal(deleteSourceResultCopy({ deleted: true, bytes_reclaimed: 2048, file_removed: true, file_missing: false }), "Source deleted. 2.0 KB reclaimed.");
equal(deleteSourceResultCopy({ deleted: true, already_deleted: true }), "This source was already deleted.");
ok(
  deleteSourceResultCopy({ deleted: true, bytes_reclaimed: 0, file_removed: false, file_missing: false })
    .includes("removed by cleanup later"),
  "locked-file outcome is reported honestly"
);
equal(deleteSourceResultCopy(null), "The source could not be deleted. Try again.");
equal(deleteSourceResultCopy({ deleted: false }), "The source could not be deleted. Try again.");

// -------------------------------------------------- fixed-copy error mapping
equal(
  storageErrorCopy(new Error(`rpc failed: cleanup_busy ${PRIVATE_SENTINEL}`)),
  "Cleanup is unavailable while a document import or text-recognition job is running. Wait for jobs to finish and try again."
);
equal(
  deleteSourceErrorCopy(new Error(`rpc failed: source_busy ${PRIVATE_SENTINEL}`)),
  "This source is busy with a background job that could not be stopped in time. Try deleting it again in a moment."
);
equal(
  deleteSourceErrorCopy(new Error(`delete_failed ${PRIVATE_SENTINEL}`)),
  "The source could not be deleted. Nothing was removed. Try again."
);
// Arbitrary backend errors — including hostile content — collapse to fixed copy.
for (const hostile of [new Error(`C:\\Users\\victim\\${PRIVATE_SENTINEL}.pdf`), PRIVATE_SENTINEL, { weird: true }, undefined]) {
  const storageCopy = storageErrorCopy(hostile);
  const deleteCopy = deleteSourceErrorCopy(hostile);
  equal(storageCopy, "Something went wrong with storage. Try refreshing.");
  equal(deleteCopy, "The source could not be deleted. Try again.");
  ok(!storageCopy.includes(PRIVATE_SENTINEL));
  ok(!deleteCopy.includes(PRIVATE_SENTINEL));
}

console.log(`test-storage-ui: ${assertionCount} assertions passed`);
