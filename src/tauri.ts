import { invoke } from "@tauri-apps/api/core";

export type AppStatus = {
  profile_id: string;
  profile_dir: string;
  backend_ready: boolean;
  backend_degraded: boolean;
};

export type Settings = {
  default_model?: string;
  ollama_endpoint?: string;
  embedding_backend?: string;
  embedding_model?: string;
  vision_backend?: VisionBackend;
  [key: string]: unknown;
};

export type JobScope = "library" | "session";

export type JobRecord = {
  job_id: string;
  kind: string;
  state: string;
  message_code: string;
  scope: JobScope;
  document_id: string;
  artifact_id: string;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  elapsed_ms: number;
  queue_position: number | null;
};

export type SubmitImportJobsResult = { jobs: JobRecord[] };

export type Session = {
  id: string;
  title: string;
  model: string;
  created_at: number;
  updated_at: number;
  last_message_at: number | null;
};

export type OperationTrace = {
  schema_version: 1;
  timing: {
    answer_latency_ms?: number;
    synthesis_elapsed_ms?: number;
    preprocessing_elapsed_ms?: number;
    vision_elapsed_ms?: number;
    verifier_latency_ms?: number;
    correction_latency_ms?: number;
    total_vision_elapsed_ms?: number;
  };
  models: {
    final_answer_model?: string;
    vision_backend?: string;
    vision_model?: string;
    ocr_engine?: string;
    embedding_model?: string;
    embedding_backend?: string;
  };
  pipeline: {
    rag_enabled: boolean;
    rag_preset?: string;
    verifier_enabled: boolean;
    verifier_status?: string;
    requested_multimodal_mode?: string;
    executed_multimodal_mode?: string;
    visual_evidence_reused?: boolean;
    vision_rerun?: boolean;
    context_evidence_action?: string;
    deterministic_visual_answer?: boolean;
    answer_style?: string;
    thinking_mode?: string;
    done_reason?: string;
  };
  tokens: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_duration_ns?: number;
    load_duration_ns?: number;
    generation_tokens_per_second?: number;
  };
  sources: {
    retrieved_chunk_ids: string[];
    retrieved_document_ids: string[];
    retrieved_page_numbers: number[];
  };
  warnings: string[];
  model_trace: {
    thinking_returned: boolean;
    thinking_char_count: number;
    thinking_truncated: boolean;
  };
};

export type Message = {
  id: string;
  session_id: string;
  role: "system" | "user" | "assistant";
  content: string;
  created_at: number;
  metadata?: {
    operation_trace?: OperationTrace;
    [key: string]: unknown;
  };
  documents?: DocumentRecord[];
  artifacts?: ArtifactRecord[];
  attachments?: MessageAttachment[];
};

export type OllamaStatus = {
  name: "ollama";
  installed: boolean;
  reachable: boolean;
  endpoint: string;
  version: string;
  models: string[];
  model_details?: OllamaModelInfo[];
  conversation_models?: ConversationModelCatalogItem[];
  error: string;
  updated_at: number;
};

export type ConversationModelCatalogItem = {
  tag: string;
  canonical_tag: string;
  display_name: string;
  role: "chat" | "vision" | "embedding" | "unknown";
  installed: boolean;
  stale: boolean;
  exact_tags: string[];
  tooltip: string;
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
  assistant_message: Message | null;
  messages: Message[];
  retrieved_chunks: RAGSearchResult[];
  retrieved_snippets: RAGSnippet[];
  grounding: RAGGroundingReport;
  answer_style: AnswerStyle;
  rag_preset: RagPreset;
  document_evidence?: DocumentEvidenceDiagnostic[];
  artifact_analysis?: ArtifactAnalysisRun;
  attached_artifacts?: ArtifactRecord[];
  attached_documents?: DocumentRecord[];
};

export type DocumentEvidenceDiagnostic = {
  document_id: string;
  display_name?: string;
  file_type: string;
  index_status: string;
  ocr_status: string;
  ocr_error: string;
  is_low_text: boolean;
  page_count: number;
  pages_with_extracted_text: number;
  low_text_page_count: number;
  extracted_text_char_count: number;
  ocr_page_count: number;
  ocr_pages_with_text: number;
  ocr_text_char_count: number;
  chunk_count: number;
  needs_ocr: boolean;
  evidence_action?: string;
  session_attachment_evidence_used?: boolean;
  indexed_source_evidence_used?: boolean;
  model_received_document_evidence?: boolean;
  ocr_stats?: Record<string, unknown>;
  before?: Record<string, unknown>;
  retrieved_pages?: number[];
  ocr_query_terms?: string[];
  ocr_exact_matches?: string[];
  ocr_fuzzy_matches?: OCRFuzzyMatch[];
  ocr_context_notes?: string[];
  ocr_line_windows?: OCRLineWindow[];
  ocr_quality?: string;
  ocr_quality_counts?: Record<string, number>;
  ocr_attempt_count?: number;
  ocr_crop_count?: number;
  ocr_selected_attempts?: Array<Record<string, unknown>>;
  ocr_crop_evidence?: Array<Record<string, unknown>>;
  vlm_text_available?: boolean;
  vlm_text_char_count?: number;
  vlm_text_backends?: string[];
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
  is_internal?: boolean;
  scope?: SourceScope;
  promoted_at?: number | null;
  display_order?: number;
  status_label?: string;
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
  ocr_query_terms?: string[];
  ocr_exact_matches?: string[];
  ocr_fuzzy_matches?: OCRFuzzyMatch[];
  ocr_substring_matches?: string[];
  ocr_context_notes?: string[];
  ocr_line_windows?: OCRLineWindow[];
  ocr_confidence?: number | null;
  ocr_quality?: Record<string, unknown>;
  ocr_quality_label?: string;
  ocr_attempt_count?: number;
  ocr_crop_count?: number;
  crop_evidence?: Array<Record<string, unknown>>;
  selected_ocr_attempt?: Record<string, unknown>;
  vlm_text_evidence?: Record<string, unknown>;
  vlm_text_available?: boolean;
  vlm_text_char_count?: number;
};

export type OCRFuzzyMatch = {
  kind?: string;
  term?: string;
  matched_text?: string;
  distance?: number;
};

export type OCRLineWindow = {
  page?: number;
  line_start?: number;
  line_end?: number;
  text?: string;
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
  subprocess_timeout_seconds?: number;
};

export type ArtifactSourceKind =
  | "file"
  | "clipboard"
  | "screenshot_full"
  | "screenshot_window"
  | "screenshot_region"
  | "derived_crop";

export type ArtifactDerivationKind =
  | "normalized_image"
  | "thumbnail"
  | "crop"
  | "ocr_text"
  | "vision_observations"
  | "combined_evidence";

export type MultimodalMode = "automatic" | "ocr_only" | "vision_only" | "combined";
export type VisionBackend = "automatic" | "florence2" | "ollama" | "ocr_only";

export type SourceScope = "library" | "session";
export type SourceBackendKind = "document" | "artifact";
export type SourceType = "pdf" | "text" | "markdown" | "image" | "screenshot";

export type SourceSummary = {
  id: string;
  backend_kind: SourceBackendKind;
  source_type: SourceType;
  scope: SourceScope;
  display_name: string;
  mime_type: string;
  size_bytes: number;
  created_at: number;
  updated_at: number;
  processing_status: string;
  indexing_status: string;
  thumbnail_path?: string;
  page_count?: number;
  extracted_text_char_count?: number;
  ocr_text_char_count?: number;
  ocr_pages_with_text?: number;
  chunk_count?: number;
  diagnostics?: DocumentEvidenceDiagnostic;
  width?: number;
  height?: number;
  warning?: string;
  error?: string;
  conversation_status?: "in_conversation";
  conversation_added_message_id?: string;
  conversation_created_at?: number;
  document?: DocumentRecord;
  artifact?: ArtifactRecord;
};

export type MessageAttachment = (DocumentRecord & { backend_kind: "document" }) | (ArtifactRecord & { backend_kind: "artifact" });

export type SourceImportItem = {
  source: SourceSummary;
  document?: DocumentRecord;
  artifact?: ArtifactRecord;
  index?: RAGIndexResult | null | Record<string, unknown>;
};

export type SourceImportResult = SourceImportItem | {
  imported: SourceImportItem[];
  sources: SourceSummary[];
  failed: Array<{ name: string; error: string }>;
};

export type ArtifactRecord = {
  id: string;
  kind: "image";
  source_kind: ArtifactSourceKind;
  name: string;
  original_filename: string;
  mime_type: string;
  original_extension: string;
  content_hash: string;
  size_bytes: number;
  width: number;
  height: number;
  normalized_orientation: boolean;
  status: string;
  error: string;
  is_deleted: boolean;
  created_at: number;
  updated_at: number;
  original_format?: string;
  original_color_mode?: string;
  original_pixel_count?: number;
  original_has_alpha?: boolean;
  original_exif_orientation?: string;
  preprocessing_version?: string;
  scope?: SourceScope;
  promoted_at?: number | null;
  source_path_redacted?: boolean;
  thumbnail_path?: string;
  display_order?: number;
  status_label?: string;
  derivations?: ArtifactDerivation[];
};

export type ArtifactDerivation = {
  id: string;
  artifact_id: string;
  kind: ArtifactDerivationKind;
  stored_path: string;
  text_content: string;
  content_hash: string;
  producer_type: string;
  producer_name: string;
  producer_version: string;
  producer_model: string;
  prompt_version: string;
  confidence: number | null;
  metadata: Record<string, unknown>;
  status: string;
  error: string;
  created_at: number;
  updated_at: number;
};

export type ArtifactAnalysisRun = {
  id: string;
  request_id: string;
  artifact_id: string;
  mode: MultimodalMode;
  user_question: string;
  requested_vision_backend: VisionBackend | "";
  actual_vision_backend: VisionBackend | "";
  requested_vision_model: string;
  actual_vision_model: string;
  ocr_engine: string;
  prompt_version: string;
  status: "running" | "pending" | "completed" | "completed_with_warnings" | "error" | "timeout" | "interrupted";
  stage: string;
  warnings: string[];
  error: string;
  output: Record<string, unknown>;
  evidence: Record<string, unknown>;
  timings: Record<string, unknown>;
  created_at: number;
  started_at: number;
  completed_at: number | null;
};

export type ArtifactImportManyResult = {
  imported: ArtifactRecord[];
  failed: Array<{ path: string; error: string }>;
};

export type ArtifactDiagnostics = {
  supported_formats: string[];
  max_original_bytes: number;
  max_decoded_pixels: number;
  max_images_per_turn: number;
  preprocessing_version?: string;
  thumbnail_max_edge: number;
  vision_max_edge?: number;
  vision_max_pixels?: number;
  vision_jpeg_quality?: number;
  ocr_max_edge?: number;
  normalized_max_edge: number;
  storage_root: string;
  artifact_count: number;
  derivation_count: number;
  vision_derivative_count?: number;
  ocr_derivative_count?: number;
  interrupted_or_error_analysis_count: number;
  rag_source_count: number;
};

export type ModelCapabilityState = "yes" | "no" | "unknown" | "error" | "stale";

export type ModelCapability = {
  model: string;
  digest: string;
  size: number;
  family: string;
  parameter_size: string;
  quantization_level: string;
  context_length: number;
  capabilities: string[];
  text_generation: ModelCapabilityState;
  vision: ModelCapabilityState;
  embedding: ModelCapabilityState;
  tools: ModelCapabilityState;
  thinking: ModelCapabilityState;
  raw: Record<string, unknown>;
  inspected_at: number;
  error: string;
};

export type ImageVisionDiagnostics = {
  artifacts: ArtifactDiagnostics;
  model_capabilities: ModelCapability[];
  florence?: FlorenceDiagnostics;
  capture: CaptureCapabilities;
  sources?: {
    library_count: number;
    session_count: number;
  };
};

export type SidecarLaunchInfo = {
  profile_dir?: string;
  profile_dir_env?: string;
  python_executable?: string;
  cwd?: string;
  resource_dir?: string;
  dev_repo_root?: string;
  app_data_dir?: string;
  florence_model_dir_env?: string;
  florence_model_dir_env_state?: string;
  copied_backend_dir?: string;
};

export type FlorencePackCandidate = {
  source: string;
  path: string;
  exists: boolean;
  is_dir: boolean;
  manifest_path?: string;
  manifest_present?: boolean;
  manifest_parsed?: boolean;
  manifest_valid?: boolean;
  files_valid?: boolean;
  checksums_valid?: boolean | "not_checked";
  hashes_checked?: boolean;
  ready: boolean;
  failed_stage?: string;
  rejection_reason?: string;
};

export type FlorenceDiagnostics = {
  pack_id: string;
  model_id: string;
  revision: string;
  license: string;
  pack_dir: string;
  state: string;
  ready: boolean;
  pack_ready?: boolean;
  runtime_ready?: boolean;
  message: string;
  failed_stage?: string;
  normal_runtime_downloads: boolean;
  trust_remote_code: boolean;
  dependencies: Record<string, unknown>;
  runtime_pins: Record<string, string>;
  native_class_status: string;
  required_files: string[];
  hashes_checked: boolean;
  loaded: boolean;
  selected_pack_path?: string;
  selected_pack_source?: string;
  searched_candidates?: FlorencePackCandidate[];
  path_context?: Record<string, string>;
  python_executable?: string;
  stages?: Record<string, Record<string, unknown>>;
  manifest?: {
    pack_id?: string;
    model_id?: string;
    revision?: string;
    created_at?: string;
    file_count?: number;
  };
};

export type CaptureCapabilities = {
  platform: string;
  full_screen: boolean;
  region: boolean;
  window: boolean;
  clipboard_image?: boolean;
  message: string;
};

export type CaptureResult = {
  path: string;
  source_kind: ArtifactSourceKind;
  width: number;
  height: number;
  bytes: number;
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
  python_executable?: string;
  sidecar?: SidecarLaunchInfo;
  current_model: string;
  settings: Settings;
  ollama: OllamaStatus;
  ollama_ps?: OllamaPsStatus;
  ocr: OCRStatus;
  rag: RAGHealth;
  image_vision?: ImageVisionDiagnostics;
};

export type ImageEvalCaseSummary = {
  id: string;
  category: string;
  modes: MultimodalMode[];
  question: string;
  image: string;
};

export type ImageEvalSuite = {
  suite_name: string;
  suite_version: string;
  prompt_version: string;
  cases_dir: string;
  case_count: number;
  cases: ImageEvalCaseSummary[];
};

export type ImageEvalCaseResult = {
  id: string;
  run_id: string;
  case_id: string;
  category: string;
  mode: MultimodalMode;
  status: string;
  passed: boolean;
  grader_review_required: boolean;
  reasons: string[];
  raw_output: string;
  structured_output: Record<string, unknown>;
  assertion_matches: Array<Record<string, unknown>>;
  warnings: string[];
  error: string;
  latency_ms: number;
  image_hash: string;
  image_width: number;
  image_height: number;
  created_at: number;
};

export type ImageEvalRun = {
  id: string;
  suite_name: string;
  suite_version: string;
  prompt_version: string;
  mode: MultimodalMode;
  model: string;
  ocr_engine: string;
  total_passed: number;
  total_failed: number;
  grader_review_count: number;
  timeout_count: number;
  runtime_error_count: number;
  total_runtime_ms: number;
  status: string;
  notes: string;
  created_at: number;
  completed_at: number | null;
  cases: ImageEvalCaseResult[];
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

export async function retryBackend(): Promise<void> {
  return invoke<void>("retry_backend");
}

export async function submitImportJobs(paths: string[], scope: JobScope): Promise<JobRecord[]> {
  const result = await rpc<SubmitImportJobsResult>("jobs.submit_import", { paths, scope });
  return result.jobs;
}

export async function getJob(jobId: string): Promise<JobRecord> {
  return rpc<JobRecord>("jobs.get", { job_id: jobId });
}

export async function listJobs(): Promise<JobRecord[]> {
  return rpc<JobRecord[]>("jobs.list");
}

export async function cancelJob(jobId: string): Promise<JobRecord> {
  return rpc<JobRecord>("jobs.cancel", { job_id: jobId });
}

export async function rpc<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  return invoke<T>("rpc_call", { method, params });
}
