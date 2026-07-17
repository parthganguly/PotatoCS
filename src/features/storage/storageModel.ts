/**
 * Fixed-copy storage view model. Raw backend errors, paths, and payload text
 * never reach the UI by construction — every user-visible string is fixed
 * vocabulary defined here (same discipline as jobModel.ts).
 */

export type StorageCategoryKey = "database" | "documents" | "images" | "logs" | "models" | "other";

export type StorageCategory = { bytes: number; files: number };

export type StorageStatus = {
  profile_dir: string;
  total_bytes: number;
  categories: Record<StorageCategoryKey, StorageCategory>;
  skipped_count: number;
  free_disk_bytes: number;
  low_disk: boolean;
  low_disk_threshold_bytes: number;
  source_counts: { documents: number; images: number };
};

export type CleanupPreview = {
  embedding_cache: { rows: number; bytes: number };
  orphan_files: { files: number; bytes: number; skipped: number };
  legacy_deleted_sources: {
    documents: number;
    artifacts: number;
    rows: number;
    files: number;
    bytes: number;
  };
  reclaimable_file_bytes: number;
  reclaimable_db_bytes: number;
};

export type CleanupResult = {
  embedding_cache: { rows: number; bytes: number };
  orphan_files: { files: number; bytes: number; skipped: number; failed: number };
  legacy_deleted_sources: {
    documents: number;
    artifacts: number;
    rows: number;
    files: number;
    bytes: number;
    skipped: number;
    failed: number;
  };
  reclaimed_file_bytes: number;
  reclaimed_db_bytes: number;
  skipped_count: number;
  failed_count: number;
};

export type DeleteSourceResult = {
  deleted?: boolean;
  already_deleted?: boolean;
  tombstoned?: boolean;
  file_removed?: boolean;
  file_missing?: boolean;
  bytes_reclaimed?: number;
  files_removed?: number;
  failed_files?: number;
};

export type StorageCategoryRow = {
  key: StorageCategoryKey;
  label: string;
  explanation: string;
  bytes: number;
  files: number;
};

const CATEGORY_LABELS: Record<StorageCategoryKey, { label: string; explanation: string }> = {
  documents: {
    label: "Documents",
    explanation: "Copies of documents you imported. Deleting a source removes its copy."
  },
  images: {
    label: "Images",
    explanation: "Imported images, screenshots, and their processed versions."
  },
  database: {
    label: "App database",
    explanation: "Search index, chat history, and settings (includes the database journal files)."
  },
  logs: {
    label: "Logs",
    explanation: "Technical logs, capped at a few megabytes."
  },
  models: {
    label: "AI model files",
    explanation: "Optional local AI model files. Never touched by cleanup."
  },
  other: {
    label: "Other",
    explanation: "Small app files that do not fit the groups above."
  }
};

const CATEGORY_ORDER: StorageCategoryKey[] = [
  "documents",
  "images",
  "database",
  "models",
  "logs",
  "other"
];

const KNOWN_ERROR_COPY: Record<string, string> = {
  cleanup_busy: "Cleanup is unavailable while a document import or text-recognition job is running. Wait for jobs to finish and try again.",
  source_busy: "This source is busy with a background job that could not be stopped in time. Try deleting it again in a moment.",
  delete_failed: "The source could not be deleted. Nothing was removed. Try again."
};

const GENERIC_STORAGE_ERROR = "Something went wrong with storage. Try refreshing.";
const GENERIC_DELETE_ERROR = "The source could not be deleted. Try again.";

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "unknown";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = "B";
  for (const next of units) {
    if (value < 1024) break;
    value /= 1024;
    unit = next;
  }
  const rounded = value >= 100 ? Math.round(value).toString() : value.toFixed(1);
  return `${rounded} ${unit}`;
}

export function storageCategoryRows(status: StorageStatus): StorageCategoryRow[] {
  return CATEGORY_ORDER.map((key) => {
    const bucket = status.categories?.[key] ?? { bytes: 0, files: 0 };
    return {
      key,
      label: CATEGORY_LABELS[key].label,
      explanation: CATEGORY_LABELS[key].explanation,
      bytes: Number(bucket.bytes) || 0,
      files: Number(bucket.files) || 0
    };
  });
}

export function lowDiskCopy(status: StorageStatus): string {
  if (status.free_disk_bytes < 0) {
    return "Free space on this drive could not be measured.";
  }
  if (status.low_disk) {
    return `Low disk space: only ${formatBytes(status.free_disk_bytes)} free on this drive. Delete sources you no longer need or run cleanup.`;
  }
  return `${formatBytes(status.free_disk_bytes)} free on this drive.`;
}

export function skippedCopy(skippedCount: number): string | null {
  if (skippedCount <= 0) return null;
  return `${skippedCount} item${skippedCount === 1 ? "" : "s"} could not be measured and ${skippedCount === 1 ? "is" : "are"} not included in the totals.`;
}

export function cleanupPreviewCopy(preview: CleanupPreview): string {
  const fileBytes = Number(preview.reclaimable_file_bytes) || 0;
  const dbBytes = Number(preview.reclaimable_db_bytes) || 0;
  if (fileBytes <= 0 && dbBytes <= 0) {
    return "Nothing to clean up right now.";
  }
  const parts: string[] = [];
  if (fileBytes > 0) {
    parts.push(`${formatBytes(fileBytes)} of leftover files can be removed`);
  }
  if (dbBytes > 0) {
    parts.push(
      `${formatBytes(dbBytes)} inside the app database can be made reusable (the database file itself does not shrink)`
    );
  }
  return `${parts.join(", and ")}.`;
}

export function cleanupConfirmCopy(preview: CleanupPreview): string {
  return (
    `Run cleanup? ${cleanupPreviewCopy(preview)}\n\n` +
    "Cleanup only removes leftover app data: unused search-index entries, orphaned app file copies, and data from previously deleted sources. " +
    "Your imported sources, chat history, settings, and any files outside this app are not touched."
  );
}

export function cleanupResultCopy(result: CleanupResult): string {
  const fileBytes = Number(result.reclaimed_file_bytes) || 0;
  const dbBytes = Number(result.reclaimed_db_bytes) || 0;
  const failed = Number(result.failed_count) || 0;
  const skipped = Number(result.skipped_count) || 0;
  const parts: string[] = [];
  if (fileBytes > 0) parts.push(`${formatBytes(fileBytes)} of files removed`);
  if (dbBytes > 0) parts.push(`${formatBytes(dbBytes)} made reusable inside the app database`);
  if (parts.length === 0) parts.push("nothing needed cleaning");
  let copy = `Cleanup finished: ${parts.join(", ")}.`;
  if (failed > 0) {
    copy += ` ${failed} item${failed === 1 ? "" : "s"} could not be removed and will be retried next time.`;
  }
  if (skipped > 0) {
    copy += ` ${skipped} item${skipped === 1 ? "" : "s"} were skipped.`;
  }
  return copy;
}

export function deleteSourceConfirmCopy(displayName: string): string {
  return (
    `Delete "${displayName}"?\n\n` +
    "This removes the app's local copy of this source and its search index. " +
    "Your original file outside this app is not touched."
  );
}

export function deleteSourceResultCopy(result: DeleteSourceResult | null | undefined): string {
  if (!result || result.deleted !== true) return GENERIC_DELETE_ERROR;
  if (result.already_deleted) {
    return "This source was already deleted.";
  }
  const bytes = Number(result.bytes_reclaimed) || 0;
  let copy = bytes > 0 ? `Source deleted. ${formatBytes(bytes)} reclaimed.` : "Source deleted.";
  if (result.file_removed === false && result.file_missing === false) {
    copy += " Its file copy is still in use and will be removed by cleanup later.";
  }
  return copy;
}

/**
 * Fixed error mapping. Known backend codes map to fixed copy; anything else —
 * including raw error strings — collapses to a generic fixed message.
 */
export function storageErrorCopy(error: unknown): string {
  const text = error instanceof Error ? error.message : typeof error === "string" ? error : "";
  for (const code of Object.keys(KNOWN_ERROR_COPY)) {
    if (text.includes(code)) return KNOWN_ERROR_COPY[code];
  }
  return GENERIC_STORAGE_ERROR;
}

export function deleteSourceErrorCopy(error: unknown): string {
  const text = error instanceof Error ? error.message : typeof error === "string" ? error : "";
  for (const code of ["source_busy", "delete_failed"]) {
    if (text.includes(code)) return KNOWN_ERROR_COPY[code];
  }
  return GENERIC_DELETE_ERROR;
}
