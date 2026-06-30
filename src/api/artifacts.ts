import {
  ArtifactAnalysisRun,
  ArtifactDerivation,
  ArtifactImportManyResult,
  ArtifactRecord,
  ArtifactSourceKind,
  MultimodalMode,
  VisionBackend,
  rpc
} from "../tauri";

export async function listArtifacts(): Promise<ArtifactRecord[]> {
  return rpc<ArtifactRecord[]>("artifacts.list");
}

export async function getArtifact(artifactId: string): Promise<ArtifactRecord> {
  return rpc<ArtifactRecord>("artifacts.get", { artifact_id: artifactId });
}

export async function importArtifactPath(path: string, sourceKind: ArtifactSourceKind = "file"): Promise<ArtifactRecord> {
  return rpc<ArtifactRecord>("artifacts.import", { path, source_kind: sourceKind });
}

export async function importArtifactPaths(
  paths: string[],
  sourceKind: ArtifactSourceKind = "file"
): Promise<ArtifactImportManyResult> {
  return rpc<ArtifactImportManyResult>("artifacts.import", { paths, source_kind: sourceKind });
}

export async function listArtifactDerivations(artifactId: string): Promise<ArtifactDerivation[]> {
  return rpc<ArtifactDerivation[]>("artifacts.derivations", { artifact_id: artifactId });
}

export async function analyzeArtifact(params: {
  artifactId: string;
  mode: MultimodalMode;
  visionBackend?: VisionBackend;
  question?: string;
  visionModel?: string;
  requestId?: string;
  cropDerivationId?: string;
}): Promise<ArtifactAnalysisRun> {
  return rpc<ArtifactAnalysisRun>("artifacts.analyze", {
    artifact_id: params.artifactId,
    mode: params.mode,
    question: params.question ?? "",
    vision_backend: params.visionBackend ?? "automatic",
    vision_model: params.visionModel ?? "",
    request_id: params.requestId ?? "",
    crop_derivation_id: params.cropDerivationId ?? ""
  });
}

export async function indexArtifactDerivation(artifactId: string, derivationId: string): Promise<Record<string, unknown>> {
  return rpc<Record<string, unknown>>("artifacts.index", {
    artifact_id: artifactId,
    derivation_id: derivationId
  });
}

export async function unindexArtifact(artifactId: string): Promise<Record<string, unknown>> {
  return rpc<Record<string, unknown>>("artifacts.unindex", { artifact_id: artifactId });
}

export async function deleteArtifact(artifactId: string): Promise<Record<string, unknown>> {
  return rpc<Record<string, unknown>>("artifacts.delete", { artifact_id: artifactId });
}
