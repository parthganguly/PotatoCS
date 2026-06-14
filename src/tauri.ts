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

export type OllamaPsStatus = {
  models: Array<{
    name: string;
    model: string;
    size: number;
    size_vram: number;
    parameter_size: string;
    quantization_level: string;
    context_length: number;
    estimated_gpu_loaded_fraction: number | null;
    estimated_cpu_loaded_fraction: number | null;
    partially_cpu_offloaded: boolean;
    warning?: string;
  }>;
  reachable: boolean;
  error: string;
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
  ollama_ps?: OllamaPsStatus;
  ocr: OCRStatus;
  rag: RAGHealth;
};

export type EvalCaseSummary = {
  id: string;
  category: string;
  difficulty: string;
  benchmark_modes: BenchmarkMode[];
  counts_toward_primary_recommendation: boolean;
  question: string;
  answer_style: AnswerStyle;
  required_source_document: string;
  expected_fact_count: number;
  forbidden_claim_count: number;
};

export type BenchmarkMode = "retrieval_only" | "oracle_generation" | "end_to_end";
export type ThinkingMode = "off" | "on" | "auto" | "legacy/unrecorded";

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
  case_category: string;
  case_difficulty: string;
  benchmark_mode: BenchmarkMode;
  thinking_mode: ThinkingMode;
  repeat_index: number;
  status: string;
  stage: string;
  pipeline_diagnosis: string;
  counts_toward_primary: boolean;
  grader_review_required: boolean;
  answer_content: string;
  thinking_text: string;
  thinking_returned: boolean;
  thinking_char_count: number;
  prompt_text: string;
  corrected_answer: string;
  model_response: Record<string, unknown>;
  retrieval_metrics: Record<string, unknown>;
  retrieval_candidates: Array<Record<string, unknown>>;
  supplied_evidence: Array<Record<string, unknown>>;
  grader_matches: Array<Record<string, unknown>>;
  timings: Record<string, unknown>;
  error_message: string;
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
  app_version: string;
  prompt_version: string;
  benchmark_mode: BenchmarkMode;
  thinking_mode: ThinkingMode;
  answer_style: string;
  status: string;
  repeat_count: number;
  num_predict: number;
  timeout_policy: Record<string, unknown>;
  model_info: Record<string, unknown>;
  retrieval_score: Record<string, unknown>;
  oracle_score: Record<string, unknown>;
  end_to_end_score: Record<string, unknown>;
  practical_score: Record<string, unknown>;
  adversarial_score: Record<string, unknown>;
  timeout_count: number;
  runtime_error_count: number;
  grader_review_count: number;
  completed_at: number | null;
  cases: EvalCaseResult[];
  summary_markdown: string;
};

export type BenchmarkComparisonGroup = {
  key: string;
  model: string;
  embedding_backend: string;
  embedding_model: string;
  benchmark_mode: BenchmarkMode;
  thinking_mode: ThinkingMode;
  prompt_version: string;
  answer_style: string;
  num_predict: number;
  status: string;
  verify: boolean;
  temperature: number;
  suite_version: string;
  run_count: number;
  repeatability_label: string;
  latest_run_passed: number;
  latest_run_failed: number;
  latest_run_grader_review: number;
  latest_run_total: number;
  latest_run_pass_rate: number;
  latest_run_coverage: number;
  latest_run_adjudicated_total: number;
  latest_run_avg_latency_ms: number;
  latest_expected_failures: number;
  latest_forbidden_failures: number;
  latest_source_failures: number;
  latest_created_at: number;
  best_run_passed: number;
  best_run_total: number;
  best_run_pass_rate: number;
  best_run_avg_latency_ms: number;
  best_created_at: number;
  mean_passed_per_run: number;
  mean_pass_rate: number;
  mean_coverage: number;
  mean_practical_pass_rate: number;
  worst_run_practical_pass_rate: number;
  mean_adversarial_pass_rate: number;
  timeout_rate: number;
  recommendation_eligible: boolean;
  median_avg_latency_ms: number;
  mean_avg_latency_ms: number;
  cumulative_passed: number;
  cumulative_total: number;
  passed: number;
  failed: number;
  grader_review: number;
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

export type BenchmarkCaseDifficultyItem = {
  case_id: string;
  question: string;
  required_source_document: string;
  attempts: number;
  passes: number;
  failures: number;
  pass_rate: number;
  source_failures: number;
  source_failure_rate: number;
  forbidden_failures: number;
  forbidden_failure_rate: number;
  observation_label: string;
};

export type BenchmarkCaseDifficulty = {
  usually_pass: BenchmarkCaseDifficultyItem[];
  usually_fail: BenchmarkCaseDifficultyItem[];
  frequent_source_failures: BenchmarkCaseDifficultyItem[];
  frequent_forbidden_failures: BenchmarkCaseDifficultyItem[];
};

export type BenchmarkComparison = {
  groups: BenchmarkComparisonGroup[];
  recommended: BenchmarkComparisonGroup | null;
  recommendation_reason: string;
  case_difficulty: BenchmarkCaseDifficulty;
  comparison_suite_version: string;
  included_run_count: number;
  excluded_run_count: number;
  incomplete_run_count: number;
  excluded_suite_versions: string[];
};

export type CampaignPreset = "quick" | "standard" | "thorough";
export type CampaignStatus =
  | "draft"
  | "queued"
  | "running"
  | "paused"
  | "completed"
  | "completed_with_errors"
  | "cancelled"
  | "interrupted"
  | "error";

export type CampaignModelInfo = {
  name: string;
  capability: "chat" | "embedding" | "unknown";
  auto_select_chat: boolean;
  parameter_size: string;
  quantization_level: string;
  size: number;
  size_vram: number;
  context_length: number;
  estimated_gpu_loaded_fraction: number | null;
  estimated_cpu_loaded_fraction: number | null;
  partially_cpu_offloaded: boolean;
  historical_average_latency_ms: number;
  warning: string;
};

export type CampaignJob = {
  id?: string;
  key: string;
  campaign_id?: string;
  sequence: number;
  model: string;
  benchmark_mode: BenchmarkMode;
  thinking_mode: Exclude<ThinkingMode, "legacy/unrecorded">;
  verify: boolean;
  repeat_count: number;
  temperature: number;
  num_predict: number;
  timeout_policy: Record<string, unknown>;
  benchmark_run_ids?: string[];
  status?: string;
  retry_count?: number;
  error?: string;
  estimated_runtime_ms: number;
  estimated_min_runtime_ms: number;
  estimate_source?: string;
  estimate_confidence?: string;
  estimate_detail?: string;
  model_info?: Partial<CampaignModelInfo>;
  warnings?: string[];
  started_at?: number | null;
  completed_at?: number | null;
};

export type CampaignPlan = {
  request: Record<string, unknown>;
  installed_models: CampaignModelInfo[];
  planned_jobs: CampaignJob[];
  planned_job_count: number;
  estimate: {
    min_ms: number;
    likely_ms: number;
    uncertain: boolean;
    job_count: number;
    model_generation_count: number;
    verifier_call_count: number;
    confidence?: string;
    source_detail?: string;
  };
  warnings: Array<{ level: string; message: string }>;
};

export type BenchmarkCampaign = {
  id: string;
  title: string;
  preset: CampaignPreset;
  app_version: string;
  suite_version: string;
  status: CampaignStatus;
  selected_models: string[];
  selected_modes: BenchmarkMode[];
  selected_thinking_modes: Array<Exclude<ThinkingMode, "legacy/unrecorded">>;
  verifier_settings: boolean[];
  repeat_count: number;
  embedding_backend: string;
  embedding_model: string;
  temperature: number;
  num_predict: number;
  timeout_policy: Record<string, unknown>;
  planned_job_count: number;
  completed_job_count: number;
  failed_job_count: number;
  timed_out_job_count: number;
  skipped_job_count: number;
  estimated_runtime_ms: number;
  estimated_min_runtime_ms: number;
  actual_runtime_ms: number;
  auto_generate_report: boolean;
  report_status: string;
  report_paths: Record<string, string>;
  report_warnings: string[];
  report_schema_version: string;
  output_folder: string;
  include_detailed_audit: boolean;
  notes: string;
  created_at: number;
  started_at: number | null;
  completed_at: number | null;
  jobs: CampaignJob[];
  current_job: CampaignJob | null;
  progress: {
    job_index: number;
    job_count: number;
    completed_terminal_jobs: number;
    elapsed_ms: number;
    estimated_remaining_ms: number;
  };
};

export type CampaignReportResult = {
  status: string;
  paths: Record<string, string>;
  warnings: string[];
  report_schema_version: string;
  screenshot_manifest: Array<Record<string, unknown>>;
  pdf_status: string;
};

export type CampaignReportData = {
  report_schema_version: string;
  report_status?: string;
  report_files?: Record<string, string>;
  campaign: Record<string, unknown>;
  application: Record<string, unknown>;
  eval_suite: Record<string, unknown>;
  runtime: Record<string, unknown>;
  embedding: Record<string, unknown>;
  job_matrix: CampaignJob[];
  benchmark_runs: EvalRun[];
  comparison: BenchmarkComparison;
  recommendation: Record<string, BenchmarkComparisonGroup | Record<string, unknown> | string | null>;
  case_difficulty: BenchmarkCaseDifficulty;
  pipeline_diagnoses: Record<string, number>;
  timeouts_errors: Record<string, unknown>;
  report_generation: Record<string, unknown>;
  screenshot_manifest: Array<Record<string, unknown>>;
  view_model?: Record<string, unknown>;
};

export async function getAppStatus(): Promise<AppStatus> {
  return invoke<AppStatus>("app_status");
}

export async function rpc<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  return invoke<T>("rpc_call", { method, params });
}
