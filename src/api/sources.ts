import { SourceBackendKind, SourceImportResult, SourceScope, SourceSummary, rpc } from "../tauri";

export async function listSources(params: {
  scope?: SourceScope;
  includeSession?: boolean;
  filter?: string;
} = {}): Promise<SourceSummary[]> {
  return rpc<SourceSummary[]>("sources.list", {
    scope: params.scope ?? "library",
    include_session: params.includeSession ?? false,
    filter: params.filter ?? "all"
  });
}

export async function importSources(params: {
  paths: string[];
  scope: SourceScope;
  index?: boolean;
  sourceKind?: string;
}): Promise<SourceImportResult> {
  return rpc<SourceImportResult>("sources.import", {
    paths: params.paths,
    scope: params.scope,
    index: params.index ?? true,
    source_kind: params.sourceKind ?? "file"
  });
}

export async function importSource(params: {
  path: string;
  scope: SourceScope;
  index?: boolean;
  sourceKind?: string;
}): Promise<SourceImportResult> {
  return rpc<SourceImportResult>("sources.import", {
    path: params.path,
    scope: params.scope,
    index: params.index ?? true,
    source_kind: params.sourceKind ?? "file"
  });
}

export async function promoteSource(params: {
  backendKind: SourceBackendKind;
  sourceId: string;
  index?: boolean;
  derivationId?: string;
}): Promise<SourceImportResult> {
  return rpc<SourceImportResult>("sources.promote", {
    backend_kind: params.backendKind,
    source_id: params.sourceId,
    index: params.index ?? false,
    derivation_id: params.derivationId ?? ""
  });
}

export async function deleteSource(backendKind: SourceBackendKind, sourceId: string): Promise<Record<string, unknown>> {
  return rpc<Record<string, unknown>>("sources.delete", {
    backend_kind: backendKind,
    source_id: sourceId
  });
}

export async function conversationSources(sessionId: string): Promise<SourceSummary[]> {
  return rpc<SourceSummary[]>("sources.conversation_context", {
    session_id: sessionId
  });
}

export async function removeConversationSource(params: {
  sessionId: string;
  backendKind: SourceBackendKind;
  sourceId: string;
}): Promise<SourceSummary[]> {
  return rpc<SourceSummary[]>("sources.remove_from_conversation", {
    session_id: params.sessionId,
    backend_kind: params.backendKind,
    source_id: params.sourceId
  });
}
