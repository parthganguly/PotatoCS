import { rpc } from "../tauri";
import type { CleanupPreview, CleanupResult, StorageStatus } from "../features/storage/storageModel";

export async function getStorageStatus(): Promise<StorageStatus> {
  return rpc<StorageStatus>("storage.status");
}

export async function getCleanupPreview(): Promise<CleanupPreview> {
  return rpc<CleanupPreview>("storage.cleanup_preview");
}

export async function runCleanup(): Promise<CleanupResult> {
  return rpc<CleanupResult>("storage.cleanup");
}
