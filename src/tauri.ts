import { invoke } from "@tauri-apps/api/core";

export type AppStatus = {
  profile_id: string;
  profile_dir: string;
  backend_ready: boolean;
};

export type Settings = {
  default_model?: string;
  ollama_endpoint?: string;
  embedding_backend?: string;
  embedding_model?: string;
  [key: string]: unknown;
};

export type Session = {
  id: string;
  title: string;
  model: string;
  created_at: number;
  updated_at: number;
  last_message_at: number | null;
};

export type Message = {
  id: string;
  session_id: string;
  role: "system" | "user" | "assistant";
  content: string;
  created_at: number;
};

export type OllamaStatus = {
  name: "ollama";
  installed: boolean;
  reachable: boolean;
  endpoint: string;
  version: string;
  models: string[];
  model_details?: OllamaModelInfo[];
  error: string;
  updated_at: number;
};

export type OllamaModelInfo = {
  name: string;
  modified_at: string;
  size: number;
  digest: string;
  format: string;
  family: string;
  parameter_size: string;
  quantization_level: string;
};

export type ChatResult = {
  session: Session;
  user_message: Message;
  assistant_message: Message;
  messages: Message[];
  retrieved_chunks: RAGSearchResult[];
  retrieved_snippets: RAGSnippet[];
  grounding: RAGGroundingReport;
  answer_style: AnswerStyle;
  rag_preset: RagPreset;
};

export type AnswerStyle = "precise" | "layman" | "detailed" | "extract_only" | "evidence_only";
export type RagPreset = "standard" | "potato";

export type DocumentRecord = {
  id: string;
  title: string;
  source_path: string;
  stored_path: string;
  file_name: string;
  file_type: string;
  content_hash: string;
  size_bytes: number;
  status: string;
  index_status: string;
  is_deleted: boolean;
  is_low_text: boolean;
  error: string;
  created_at: number;
  updated_at: number;
  indexed_at: number | null;
  indexed_embedding_model: string;
  indexed_embedding_backend: string;
  ocr_status: string;
  ocr_engine: string;
  ocr_error: string;
};

export type RAGChunk = {
  id: string;
  document_id: string;
  chunk_index: number;
  content: string;
  content_hash: string;
  page_start: number | null;
  page_end: number | null;
  metadata: Record<string, unknown>;
  embedding_model: string;
  embedding_hash: string;
  is_deleted: number;
  created_at: number;
  updated_at: number;
};

export type RAGIndexResult = {
  document: DocumentRecord;
  chunks: RAGChunk[];
  embedded: number;
  cached: number;
  low_text: boolean;
};

export type DocumentImportResult = {
  document: DocumentRecord;
  index: RAGIndexResult | null;
};

export type RAGSearchResult = {
  chunk_id: string;
  document_id: string;
  content: string;
  score: number;
  page_start: number | null;
  page_end: number | null;
  metadata: Record<string, unknown>;
};

export type RAGSnippet = {
  snippet_id: string;
  chunk_id: string;
  document_id: string;
  source: string;
  text: string;
  score: number;
  page_start: number | null;
  page_end: number | null;
  metadata: Record<string, unknown>;
};

export type RAGGroundingSource = {
  snippet_id: string;
  document_id: string;
  source: string;
  page_start: number | null;
  page_end: number | null;
  chunk_id: string;
};

export type RAGGroundingClaim = {
  text: string;
  status: "supported" | "unsupported" | "contradicted";
  reason: string;
  source_ids: string[];
};

export type RAGGroundingReport = {
  mode: "none" | "quote_first";
  sources_used: RAGGroundingSource[];
  verifier: {
    enabled: boolean;
    status: "not_run" | "no_evidence" | "passed" | "failed" | "error";
    passed: boolean | null;
    error: string;
  };
  claims: RAGGroundingClaim[];
  unsupported_claims: string[];
  contradicted_claims: string[];
  regenerated: boolean;
  draft_verifier?: RAGGroundingReport;
};

export type RAGHealth = {
  ok: boolean;
  version: string;
  documents: number;
  chunks: number;
  cached_embeddings: number;
  documents_needing_reindex: number;
  indexed_documents: number;
  documents_indexed_with_active_backend: number;
  user_documents_indexed_with_active_backend: boolean;
  embedding: EmbeddingStatus;
};

export type EmbeddingStatus = {
  backend: string;
  provider: string;
  model: string;
  cache_key: string;
  semantic: boolean;
  dimensions: number | null;
  message: string;
};

export type OCRDependencyName = "tesseract" | "pdftoppm" | "mutool";

export type OCRDependencyStatus = {
  found: boolean;
  path: string;
  source: string;
};

export type OCRStatus = {
  available: boolean;
  engine_name: string;
  renderer: string;
  message: string;
  dependencies: Record<OCRDependencyName, OCRDependencyStatus>;
};

export type OCRPage = {
  id: string;
  document_id: string;
  source_path: string;
  page_number: number;
  engine_name: string;
  confidence: number | null;
  text: string;
  text_hash: string;
  chunk_ids: string[];
  index_status: string;
  created_at: number;
  updated_at: number;
};

export type OCRStats = {
  pages_processed: number;
  pages_with_text: number;
  chunks_created: number;
  embeddings_created: number;
  embeddings_cached: number;
  warning: string;
};

export type OCRResult = {
  document: DocumentRecord;
  ocr_status: OCRStatus;
  ocr_pages: OCRPage[];
  stats: OCRStats;
  index: RAGIndexResult | null;
};

export type LegacyImportReportItem = {
  type: string;
  source: string;
  reason: string;
};

export type LegacyImportReport = {
  imported: LegacyImportReportItem[];
  skipped: LegacyImportReportItem[];
  incompatible: LegacyImportReportItem[];
  failed: LegacyImportReportItem[];
};

export type DiagnosticsStatus = {
  app_version: string;
  profile_dir: string;
  backend_ready: boolean;
  db_path: string;
  backend_log_path: string;
  current_model: string;
  settings: Settings;
  ollama: OllamaStatus;
  ocr: OCRStatus;
  rag: RAGHealth;
};

export type EvalCaseSummary = {
  id: string;
  question: string;
  answer_style: AnswerStyle;
  required_source_document: string;
  expected_fact_count: number;
  forbidden_claim_count: number;
};

export type EvalSuite = {
  suite_name: string;
  suite_version: string;
  cases_dir: string;
  case_count: number;
  cases: EvalCaseSummary[];
};

export type EvalCaseResult = {
  id: string;
  run_id: string;
  case_id: string;
  question: string;
  answer_style: AnswerStyle;
  required_source_document: string;
  passed: boolean;
  expected_passed: boolean;
  forbidden_passed: boolean;
  source_passed: boolean;
  latency_ms: number;
  reasons: string[];
  retrieved_document_ids: string[];
  retrieved_chunk_ids: string[];
  embedding_backend: string;
  embedding_model: string;
  temperature: number;
  created_at: number;
};

export type EvalRun = {
  id: string;
  model: string;
  verify: boolean;
  suite_name: string;
  suite_version: string;
  total_passed: number;
  total_failed: number;
  average_latency_ms: number;
  total_runtime_ms: number;
  embedding_backend: string;
  embedding_model: string;
  temperature: number;
  notes: string;
  created_at: number;
  cases: EvalCaseResult[];
  summary_markdown: string;
};

export type BenchmarkComparisonGroup = {
  key: string;
  model: string;
  embedding_backend: string;
  embedding_model: string;
  verify: boolean;
  temperature: number;
  suite_version: string;
  run_count: number;
  passed: number;
  total: number;
  expected_failures: number;
  forbidden_failures: number;
  source_failures: number;
  average_latency_ms: number;
  total_runtime_ms: number;
  pass_rate: number;
  guidance_labels: string[];
  verifier_recommended: boolean;
  recommended?: boolean;
};

export type BenchmarkComparison = {
  groups: BenchmarkComparisonGroup[];
  recommended: BenchmarkComparisonGroup | null;
  recommendation_reason: string;
};

export async function getAppStatus(): Promise<AppStatus> {
  return invoke<AppStatus>("app_status");
}

export async function rpc<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  return invoke<T>("rpc_call", { method, params });
}
