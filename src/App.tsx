import {
  AlertTriangle,
  BarChart3,
  Bot,
  CheckCircle2,
  CircleAlert,
  Clipboard,
  Cpu,
  Database,
  FileText,
  FolderOpen,
  History,
  Image as ImageIcon,
  MessageSquarePlus,
  Pause,
  Play,
  Power,
  RefreshCw,
  RotateCw,
  Save,
  Search,
  Send,
  Settings as SettingsIcon,
  Trash2,
  Upload,
  XCircle
} from "lucide-react";
import html2canvas from "html2canvas";
import { open } from "@tauri-apps/plugin-dialog";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { Fragment, FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import {
  AppStatus,
  AnswerStyle,
  BenchmarkCampaign,
  BenchmarkMode,
  BenchmarkCaseDifficulty,
  BenchmarkCaseDifficultyItem,
  BenchmarkComparison,
  BenchmarkComparisonGroup,
  CampaignJob,
  CampaignModelInfo,
  CampaignPlan,
  CampaignPreset,
  CampaignReportData,
  CampaignReportResult,
  ChatResult,
  ArtifactAnalysisRun,
  ArtifactDerivation,
  ArtifactRecord,
  DiagnosticsStatus,
  DocumentEvidenceDiagnostic,
  DocumentRecord,
  EmbeddingStatus,
  EvalRun,
  EvalCaseResult,
  EvalSuite,
  ImageEvalRun,
  ImageEvalSuite,
  LegacyImportReport,
  Message,
  ModelCapability,
  MultimodalMode,
  OCRDependencyName,
  OCRResult,
  OCRStatus,
  OllamaStatus,
  RAGGroundingReport,
  RAGHealth,
  RAGIndexResult,
  RAGSearchResult,
  RAGSnippet,
  RagPreset,
  Session,
  Settings,
  SourceImportResult,
  SourceSummary,
  ThinkingMode,
  VisionBackend,
  getAppStatus,
  retryBackend,
  rpc
} from "./tauri";
import {
  analyzeArtifact,
  deleteArtifact,
  getArtifact,
  importArtifactPath,
  importArtifactPaths,
  indexArtifactDerivation,
  listArtifactDerivations,
  listArtifacts,
  unindexArtifact
} from "./api/artifacts";
import { captureFullScreen, captureRegion, importClipboardImage } from "./api/capture";
import { listImageEvalHistory, listImageEvalSuite, runImageEval } from "./api/imageEvals";
import {
  buildConversationModelOptions,
  installedModelTag,
  isInstalledModelTag,
  listModelCapabilities,
  readableModelLabel,
  refreshModelCapabilities,
  visionCapableModels
} from "./api/models";
import { conversationSources, deleteSource, importSources, listSources, promoteSource, removeConversationSource } from "./api/sources";
import { ComposerAttachments, MessageAttachments, documentAttachmentStatusLabel } from "./features/attachments/ComposerAttachments";
import { ChatProgressBar } from "./features/chat/ChatProgressBar";
import { ChatHeader } from "./features/chat/ChatHeader";
import { OperationTrace } from "./features/chat/OperationTrace";
import { OperationProgressDiagnostics } from "./features/chat/OperationProgressDiagnostics";
import type { OperationProgressEvent } from "./features/chat/chatProgress";
import { useChatProgress } from "./features/chat/useChatProgress";
import { ImageBenchmarkPanel } from "./features/image-evals/ImageBenchmarkPanel";
import { JobsPanel } from "./features/jobs/JobsPanel";
import { jobSubmissionFailureCopy } from "./features/jobs/jobModel";
import { useImportJobs } from "./features/jobs/useImportJobs";
import { ArtifactAnalysisCard } from "./features/multimodal-chat/ImageAttachments";
import { ReadinessPanel } from "./features/readiness/ReadinessPanel";
import { firstRunReadinessNeeded, readinessRows } from "./features/readiness/readinessRows";
import { AppSidebar } from "./features/shell/AppSidebar";
import { backendBannerState, isBackendDegradedEvent } from "./features/shell/backendStatus";
import { SourcesPage } from "./features/sources/SourcesPage";
import { getCleanupPreview, getStorageStatus, runCleanup } from "./api/storage";
import { StoragePanel } from "./features/storage/StoragePanel";
import type { CleanupPreview, CleanupResult, StorageStatus } from "./features/storage/storageModel";
import {
  cleanupConfirmCopy,
  deleteSourceConfirmCopy,
  deleteSourceErrorCopy,
  deleteSourceResultCopy,
  storageErrorCopy
} from "./features/storage/storageModel";
import { ImageVisionDiagnostics } from "./features/vision-diagnostics/ImageVisionDiagnostics";

type LoadState = "idle" | "loading" | "error";
type ActiveView = "chat" | "sources" | "storage" | "diagnostics";
const SUPPORTED_DOCUMENT_EXTENSIONS = [".txt", ".md", ".pdf"];
const SUPPORTED_IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp"];
const ANSWER_STYLE_OPTIONS: Array<{ value: AnswerStyle; label: string }> = [
  { value: "precise", label: "Precise" },
  { value: "layman", label: "Layman" },
  { value: "detailed", label: "Detailed" },
  { value: "extract_only", label: "Extract only" },
  { value: "evidence_only", label: "Evidence only" }
];
const BENCHMARK_MODE_OPTIONS: Array<{ value: BenchmarkMode; label: string }> = [
  { value: "retrieval_only", label: "Retrieval only" },
  { value: "oracle_generation", label: "Oracle generation" },
  { value: "end_to_end", label: "End-to-end" }
];
const THINKING_MODE_OPTIONS: Array<{ value: Exclude<ThinkingMode, "legacy/unrecorded">; label: string }> = [
  { value: "off", label: "Off" },
  { value: "on", label: "On" },
  { value: "auto", label: "Auto" }
];
const CAMPAIGN_PRESET_OPTIONS: Array<{ value: CampaignPreset; label: string }> = [
  { value: "quick", label: "Quick comparison" },
  { value: "standard", label: "Standard diagnostic" },
  { value: "thorough", label: "Thorough comparison" }
];
const TERMINAL_CAMPAIGN_STATUSES = new Set(["completed", "completed_with_errors", "cancelled", "interrupted", "error"]);

function App() {
  const [appStatus, setAppStatus] = useState<AppStatus | null>(null);
  const [backendDegraded, setBackendDegraded] = useState(false);
  const [backendRetrying, setBackendRetrying] = useState(false);
  const [settings, setSettings] = useState<Settings>({});
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [ollama, setOllama] = useState<OllamaStatus | null>(null);
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactRecord[]>([]);
  const [sources, setSources] = useState<SourceSummary[]>([]);
  const [sourceFilter, setSourceFilter] = useState("all");
  const [selectedArtifactId, setSelectedArtifactId] = useState("");
  const [selectedArtifact, setSelectedArtifact] = useState<ArtifactRecord | null>(null);
  const [artifactDerivations, setArtifactDerivations] = useState<ArtifactDerivation[]>([]);
  const [modelCapabilities, setModelCapabilities] = useState<ModelCapability[]>([]);
  const [ragHealth, setRagHealth] = useState<RAGHealth | null>(null);
  const [ocrStatus, setOcrStatus] = useState<OCRStatus | null>(null);
  const [showReadiness, setShowReadiness] = useState(false);
  const [readinessChecking, setReadinessChecking] = useState(false);
  const [activeView, setActiveView] = useState<ActiveView>("chat");
  const [storageStatus, setStorageStatus] = useState<StorageStatus | null>(null);
  const [cleanupPreview, setCleanupPreview] = useState<CleanupPreview | null>(null);
  const [lastCleanup, setLastCleanup] = useState<CleanupResult | null>(null);
  const [storageLoading, setStorageLoading] = useState(false);
  const [storageCleaning, setStorageCleaning] = useState(false);
  const [storageError, setStorageError] = useState<string | null>(null);
  const [sourceNotice, setSourceNotice] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [pendingSources, setPendingSources] = useState<SourceSummary[]>([]);
  const [conversationContext, setConversationContext] = useState<SourceSummary[]>([]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [chatMultimodalMode, setChatMultimodalMode] = useState<MultimodalMode>("automatic");
  const [chatVisionModel, setChatVisionModel] = useState("");
  const [lastChatAnalysis, setLastChatAnalysis] = useState<ArtifactAnalysisRun | null>(null);
  const [modelDraft, setModelDraft] = useState("");
  const [visionBackendDraft, setVisionBackendDraft] = useState<VisionBackend>("automatic");
  const [importPath, setImportPath] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<RAGSearchResult[]>([]);
  const [retrievedChunks, setRetrievedChunks] = useState<RAGSearchResult[]>([]);
  const [retrievedSnippets, setRetrievedSnippets] = useState<RAGSnippet[]>([]);
  const [documentEvidence, setDocumentEvidence] = useState<DocumentEvidenceDiagnostic[]>([]);
  const [grounding, setGrounding] = useState<RAGGroundingReport | null>(null);
  const [useRag, setUseRag] = useState(false);
  const [verifyRag, setVerifyRag] = useState(false);
  const [answerStyle, setAnswerStyle] = useState<AnswerStyle>("precise");
  const [ragPreset, setRagPreset] = useState<RagPreset>("standard");
  const [selectedRagDocumentId, setSelectedRagDocumentId] = useState("");
  const [lastIndexResult, setLastIndexResult] = useState<RAGIndexResult | null>(null);
  const [lastOcrResult, setLastOcrResult] = useState<OCRResult | null>(null);
  const [legacyPath, setLegacyPath] = useState("");
  const [legacyReport, setLegacyReport] = useState<LegacyImportReport | null>(null);
  const [diagnostics, setDiagnostics] = useState<DiagnosticsStatus | null>(null);
  const [evalSuite, setEvalSuite] = useState<EvalSuite | null>(null);
  const [evalHistory, setEvalHistory] = useState<EvalRun[]>([]);
  const [imageEvalSuite, setImageEvalSuite] = useState<ImageEvalSuite | null>(null);
  const [imageEvalHistory, setImageEvalHistory] = useState<ImageEvalRun[]>([]);
  const [imageEvalResult, setImageEvalResult] = useState<ImageEvalRun | null>(null);
  const [imageEvalMode, setImageEvalMode] = useState<MultimodalMode>("ocr_only");
  const [imageEvalModel, setImageEvalModel] = useState("");
  const [imageCopyStatus, setImageCopyStatus] = useState("");
  const [benchmarkComparison, setBenchmarkComparison] = useState<BenchmarkComparison | null>(null);
  const [benchmarkResult, setBenchmarkResult] = useState<EvalRun | null>(null);
  const [benchmarkModel, setBenchmarkModel] = useState("");
  const [benchmarkMode, setBenchmarkMode] = useState<BenchmarkMode>("end_to_end");
  const [benchmarkThinking, setBenchmarkThinking] = useState<Exclude<ThinkingMode, "legacy/unrecorded">>("off");
  const [benchmarkVerify, setBenchmarkVerify] = useState(false);
  const [benchmarkRepeats, setBenchmarkRepeats] = useState<1 | 3>(1);
  const [campaignModels, setCampaignModels] = useState<CampaignModelInfo[]>([]);
  const [campaigns, setCampaigns] = useState<BenchmarkCampaign[]>([]);
  const [activeCampaign, setActiveCampaign] = useState<BenchmarkCampaign | null>(null);
  const [campaignPlan, setCampaignPlan] = useState<CampaignPlan | null>(null);
  const [campaignTitle, setCampaignTitle] = useState("Quick benchmark comparison");
  const [campaignPreset, setCampaignPreset] = useState<CampaignPreset>("quick");
  const [campaignSelectedModels, setCampaignSelectedModels] = useState<string[]>([]);
  const [campaignModes, setCampaignModes] = useState<BenchmarkMode[]>(["end_to_end"]);
  const [campaignThinkingModes, setCampaignThinkingModes] = useState<Array<Exclude<ThinkingMode, "legacy/unrecorded">>>(["off"]);
  const [campaignVerifierSettings, setCampaignVerifierSettings] = useState<boolean[]>([false]);
  const [campaignRepeats, setCampaignRepeats] = useState<1 | 3>(1);
  const [campaignAutoReport, setCampaignAutoReport] = useState(true);
  const [campaignOutputFolder, setCampaignOutputFolder] = useState("");
  const [campaignStatusMessage, setCampaignStatusMessage] = useState("");
  const [campaignBusy, setCampaignBusy] = useState(false);
  const [reportData, setReportData] = useState<CampaignReportData | null>(null);
  const reportViewRef = useRef<HTMLDivElement | null>(null);
  const autoReportAttempts = useRef<Set<string>>(new Set());
  const [copyStatus, setCopyStatus] = useState("");
  const [imageAnalysisMode, setImageAnalysisMode] = useState<MultimodalMode>("ocr_only");
  const [imageAnalysisQuestion, setImageAnalysisQuestion] = useState("");
  const [imageVisionModel, setImageVisionModel] = useState("");
  const [lastArtifactAnalysis, setLastArtifactAnalysis] = useState<ArtifactAnalysisRun | null>(null);
  const [captureRegionDraft, setCaptureRegionDraft] = useState({ x: 0, y: 0, width: 800, height: 600 });
  const [loadState, setLoadState] = useState<LoadState>("idle");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingUserContent, setPendingUserContent] = useState("");
  const chatProgress = useChatProgress(busy);
  const importJobs = useImportJobs(async ({ job, refreshSources: shouldRefresh }) => {
    if (!shouldRefresh) return;
    await refreshSources();
    if (job.localState === "completed" && job.target === "chat" && job.document_id) {
      const availableSources = await listSources({ scope: "session", includeSession: true, filter: "all" });
      const importedSource = availableSources.find(
        (source) => source.backend_kind === "document" && source.id === job.document_id
      );
      if (importedSource) appendPendingSources([importedSource]);
    }
  });

  const selectedSession = useMemo(
    () => sessions.find((session) => session.id === selectedSessionId) ?? null,
    [sessions, selectedSessionId]
  );
  const conversationModelOptions = useMemo(
    () => buildConversationModelOptions(ollama, modelCapabilities),
    [modelCapabilities, ollama]
  );
  const modelChoices = useMemo(
    () => conversationModelOptions.map((option) => option.tag),
    [conversationModelOptions]
  );
  const confirmedVisionModels = useMemo(() => visionCapableModels(modelCapabilities), [modelCapabilities]);

  useEffect(() => {
    void bootstrap();
  }, []);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;

    void listen<unknown>("backend_degraded", (event) => {
      if (!disposed && isBackendDegradedEvent(event.payload)) {
        setBackendDegraded(event.payload.degraded);
      }
    })
      .then((nextUnlisten) => {
        if (disposed) {
          nextUnlisten();
          return;
        }
        unlisten = nextUnlisten;
      })
      .catch(() => undefined);

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;

    void getCurrentWindow()
      .onDragDropEvent((event) => {
        if (event.payload.type !== "drop") return;
        const paths = event.payload.paths;
        if (!paths.length || disposed) return;
        void routeDroppedPaths(paths);
      })
      .then((nextUnlisten) => {
        if (disposed) {
          nextUnlisten();
          return;
        }
        unlisten = nextUnlisten;
      })
      .catch((err) => setError(readError(err)));

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [activeView]);

  useEffect(() => {
    setRetrievedChunks([]);
    setRetrievedSnippets([]);
    setDocumentEvidence([]);
    setGrounding(null);
    if (!selectedSessionId) {
      setMessages([]);
      setConversationContext([]);
      return;
    }
    void loadMessages(selectedSessionId);
    void loadConversationContext(selectedSessionId);
  }, [selectedSessionId]);

  useEffect(() => {
    if (
      selectedRagDocumentId &&
      !documents.some((document) => document.id === selectedRagDocumentId && isRagReadyDocument(document))
    ) {
      setSelectedRagDocumentId("");
    }
  }, [documents, selectedRagDocumentId]);

  useEffect(() => {
    if (activeView === "diagnostics" && !diagnostics) {
      void refreshDiagnostics();
    }
  }, [activeView, diagnostics]);

  useEffect(() => {
    if (activeView === "sources" && sources.length === 0) {
      void refreshSources();
    }
  }, [activeView]);

  useEffect(() => {
    if (activeView === "storage" && !storageStatus && !storageLoading) {
      void refreshStorage();
    }
  }, [activeView]);

  useEffect(() => {
    if (!selectedArtifactId) {
      setSelectedArtifact(null);
      setArtifactDerivations([]);
      setLastArtifactAnalysis(null);
      return;
    }
    void refreshSelectedArtifact(selectedArtifactId);
  }, [selectedArtifactId]);

  useEffect(() => {
    const defaultVision = confirmedVisionModels[0]?.model ?? "";
    if (!defaultVision) return;
    setChatVisionModel((current) => current || defaultVision);
    setImageVisionModel((current) => current || defaultVision);
    setImageEvalModel((current) => current || defaultVision);
  }, [confirmedVisionModels]);

  useEffect(() => {
    if (!activeCampaign || !["queued", "running", "paused"].includes(activeCampaign.status)) {
      return;
    }
    const timer = window.setInterval(() => {
      void refreshCampaign(activeCampaign.id, { quiet: true });
    }, 2500);
    return () => window.clearInterval(timer);
  }, [activeCampaign?.id, activeCampaign?.status]);

  useEffect(() => {
    if (!activeCampaign || !shouldAutoGenerateCampaignReport(activeCampaign)) return;
    if (autoReportAttempts.current.has(activeCampaign.id)) return;
    autoReportAttempts.current.add(activeCampaign.id);
    void generateCampaignReport(activeCampaign, { automatic: true, screenshots: true });
  }, [activeCampaign?.id, activeCampaign?.status, activeCampaign?.report_status]);

  async function bootstrap() {
    setLoadState("loading");
    setError(null);
    try {
      const [
        status,
        nextSettings,
        nextSessions,
        nextOllama,
        nextDocuments,
        nextArtifacts,
        nextSources,
        nextHealth,
        nextOcr,
        nextCapabilities,
        nextImageSuite,
        nextImageHistory
      ] =
        await Promise.all([
          getAppStatus(),
          rpc<Settings>("settings.get"),
          rpc<Session[]>("sessions.list"),
          rpc<OllamaStatus>("models.detect_ollama"),
          rpc<DocumentRecord[]>("documents.list"),
          listArtifacts(),
          listSources({ filter: sourceFilter }),
          rpc<RAGHealth>("rag.health"),
          rpc<OCRStatus>("ocr.status"),
          listModelCapabilities(false),
          listImageEvalSuite(),
          listImageEvalHistory(20)
        ]);
      setAppStatus(status);
      setBackendDegraded(Boolean(status.backend_degraded));
      setSettings(nextSettings);
      const bootModelChoices = buildConversationModelOptions(nextOllama, nextCapabilities).map((option) => option.tag);
      const bootDefaultModel = String(nextSettings.default_model ?? "llama3.2");
      setModelDraft(selectInstalledModelOrFallback(bootDefaultModel, bootModelChoices));
      setVisionBackendDraft(normalizeVisionBackend(nextSettings.vision_backend));
      setSessions(nextSessions);
      setOllama(nextOllama);
      setDocuments(nextDocuments);
      setArtifacts(nextArtifacts);
      setSources(nextSources);
      setSelectedArtifactId((current) => current || nextArtifacts[0]?.id || "");
      setRagHealth(nextHealth);
      setOcrStatus(nextOcr);
      setModelCapabilities(nextCapabilities);
      setImageEvalSuite(nextImageSuite);
      setImageEvalHistory(nextImageHistory);
      const defaultVision = visionCapableModels(nextCapabilities)[0]?.model ?? "";
      setChatVisionModel((current) => current || defaultVision);
      setImageVisionModel((current) => current || defaultVision);
      setImageEvalModel((current) => current || defaultVision);
      setSelectedSessionId((current) => current ?? nextSessions[0]?.id ?? null);
      const userDocumentCount = nextDocuments.filter(
        (document) => !document.is_deleted && !document.is_internal
      ).length;
      setShowReadiness(
        firstRunReadinessNeeded({
          sessionCount: nextSessions.length,
          userDocumentCount,
          rows: readinessRows({
            checking: false,
            backendDegraded: Boolean(status.backend_degraded),
            ollama: nextOllama,
            ragHealth: nextHealth,
            ocrStatus: nextOcr
          })
        })
      );
      setLoadState("idle");
    } catch (err) {
      setLoadState("error");
      setError(readError(err));
    }
  }

  async function retryDegradedBackend() {
    setBackendRetrying(true);
    try {
      await retryBackend();
      setBackendDegraded(false);
      setAppStatus(await getAppStatus());
    } catch {
      // Keep the fixed banner copy; raw retry errors never reach the UI.
      setBackendDegraded(true);
    } finally {
      setBackendRetrying(false);
    }
  }

  async function recheckReadiness() {
    setReadinessChecking(true);
    try {
      const status = await getAppStatus();
      setAppStatus(status);
      setBackendDegraded(Boolean(status.backend_degraded));
      const [nextOllama, nextHealth, nextOcr] = await Promise.all([
        rpc<OllamaStatus>("models.detect_ollama"),
        rpc<RAGHealth>("rag.health"),
        rpc<OCRStatus>("ocr.status")
      ]);
      setOllama(nextOllama);
      setRagHealth(nextHealth);
      setOcrStatus(nextOcr);
    } catch {
      // Rows keep rendering fixed copy from the last known statuses; raw
      // re-check errors never reach the readiness UI.
    } finally {
      setReadinessChecking(false);
    }
  }

  async function refreshDocuments() {
    const [nextDocuments, nextHealth] = await Promise.all([
      rpc<DocumentRecord[]>("documents.list"),
      rpc<RAGHealth>("rag.health")
    ]);
    setDocuments(nextDocuments);
    setRagHealth(nextHealth);
    setSources(await listSources({ filter: sourceFilter }));
  }

  async function refreshArtifacts() {
    const [nextArtifacts, nextSources] = await Promise.all([
      listArtifacts(),
      listSources({ filter: sourceFilter })
    ]);
    setArtifacts(nextArtifacts);
    setSources(nextSources);
    setSelectedArtifactId((current) =>
      current && nextArtifacts.some((artifact) => artifact.id === current) ? current : nextArtifacts[0]?.id ?? ""
    );
    return nextArtifacts;
  }

  async function refreshSources(filter = sourceFilter) {
    const [nextSources, nextDocuments, nextArtifacts, nextHealth] = await Promise.all([
      listSources({ filter }),
      rpc<DocumentRecord[]>("documents.list"),
      listArtifacts(),
      rpc<RAGHealth>("rag.health")
    ]);
    setSources(nextSources);
    setDocuments(nextDocuments);
    setArtifacts(nextArtifacts);
    setRagHealth(nextHealth);
  }

  async function refreshSelectedArtifact(artifactId: string) {
    setError(null);
    try {
      const [artifact, derivations] = await Promise.all([
        getArtifact(artifactId),
        listArtifactDerivations(artifactId)
      ]);
      setSelectedArtifact(artifact);
      setArtifactDerivations(derivations);
    } catch (err) {
      setSelectedArtifact(null);
      setArtifactDerivations([]);
      setError(readError(err));
    }
  }

  async function refreshOllama() {
    setError(null);
    try {
      const nextOllama = await rpc<OllamaStatus>("models.detect_ollama");
      const nextCapabilities = await listModelCapabilities(false);
      const refreshedChoices = buildConversationModelOptions(nextOllama, nextCapabilities).map((option) => option.tag);
      setOllama(nextOllama);
      setModelCapabilities(nextCapabilities);
      setModelDraft((current) => selectInstalledModelOrFallback(current, refreshedChoices));
    } catch (err) {
      setError(readError(err));
    }
  }

  async function refreshOcrStatus() {
    setError(null);
    try {
      setOcrStatus(await rpc<OCRStatus>("ocr.status"));
    } catch (err) {
      setError(readError(err));
    }
  }

  async function refreshDiagnostics() {
    setBusy(true);
    setError(null);
    try {
      const [
        nextDiagnostics,
        nextSuite,
        nextHistory,
        nextComparison,
        nextCampaignModels,
        nextCampaigns,
        nextImageSuite,
        nextImageHistory
      ] = await Promise.all([
        rpc<DiagnosticsStatus>("diagnostics.get"),
        rpc<EvalSuite>("evals.list"),
        rpc<EvalRun[]>("evals.history", { limit: 20 }),
        rpc<BenchmarkComparison>("evals.comparison", { limit: 100 }),
        rpc<CampaignModelInfo[]>("campaigns.models"),
        rpc<BenchmarkCampaign[]>("campaigns.list", { limit: 20 }),
        listImageEvalSuite(),
        listImageEvalHistory(20)
      ]);
      setDiagnostics(nextDiagnostics);
      setEvalSuite(nextSuite);
      setEvalHistory(nextHistory);
      setImageEvalSuite(nextImageSuite);
      setImageEvalHistory(nextImageHistory);
      setModelCapabilities(nextDiagnostics.image_vision?.model_capabilities ?? []);
      setBenchmarkComparison(nextComparison);
      setCampaignModels(nextCampaignModels);
      setCampaigns(nextCampaigns);
      setActiveCampaign((current) => {
        if (!current) return nextCampaigns[0] ?? null;
        return nextCampaigns.find((campaign) => campaign.id === current.id) ?? current;
      });
      setOllama(nextDiagnostics.ollama);
      setOcrStatus(nextDiagnostics.ocr);
      setRagHealth(nextDiagnostics.rag);
      const diagnosticModelChoices = buildConversationModelOptions(
        nextDiagnostics.ollama,
        nextDiagnostics.image_vision?.model_capabilities ?? []
      ).map((option) => option.tag);
      setModelDraft((current) => selectInstalledModelOrFallback(current, diagnosticModelChoices));
      const defaultVision = visionCapableModels(nextDiagnostics.image_vision?.model_capabilities ?? [])[0]?.model ?? "";
      setChatVisionModel((current) => current || defaultVision);
      setImageVisionModel((current) => current || defaultVision);
      setImageEvalModel((current) => current || defaultVision);
      setBenchmarkModel((current) =>
        nextDiagnostics.ollama.models.includes(current)
          ? current
          : nextDiagnostics.ollama.models[0] || ""
      );
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function runBenchmark() {
    const model = benchmarkModel.trim();
    if (!model) return;
    setBusy(true);
    setError(null);
    setCopyStatus("");
    try {
      const run = await rpc<EvalRun>("evals.run", {
        model,
        benchmark_mode: benchmarkMode,
        thinking_mode: benchmarkThinking,
        verify: benchmarkMode === "end_to_end" ? benchmarkVerify : false,
        repeats: benchmarkRepeats
      });
      setBenchmarkResult(run);
      const [nextHistory, nextComparison] = await Promise.all([
        rpc<EvalRun[]>("evals.history", { limit: 20 }),
        rpc<BenchmarkComparison>("evals.comparison", { limit: 100 })
      ]);
      setEvalHistory(nextHistory);
      setBenchmarkComparison(nextComparison);
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function copyBenchmarkSummary() {
    const summary = benchmarkSummaryMarkdown(benchmarkResult ? [benchmarkResult] : evalHistory);
    if (!summary.trim()) return;
    setError(null);
    try {
      await navigator.clipboard.writeText(summary);
      setCopyStatus("Copied benchmark summary.");
    } catch (err) {
      setError(readError(err));
    }
  }

  async function clearBenchmarkHistory() {
    setBusy(true);
    setError(null);
    setCopyStatus("");
    try {
      await rpc("evals.clear_history");
      setBenchmarkResult(null);
      setEvalHistory([]);
      setBenchmarkComparison(null);
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  function campaignRequest(): Record<string, unknown> {
    return {
      title: campaignTitle,
      preset: campaignPreset,
      models: campaignSelectedModels,
      modes: campaignModes,
      thinking_modes: campaignThinkingModes,
      verifier_settings: campaignVerifierSettings,
      repeats: campaignRepeats,
      auto_generate_report: campaignAutoReport,
      output_folder: campaignOutputFolder
    };
  }

  async function reviewCampaignPlan() {
    setCampaignBusy(true);
    setError(null);
    setCampaignStatusMessage("");
    try {
      const plan = await rpc<CampaignPlan>("campaigns.plan", campaignRequest());
      setCampaignPlan(plan);
      setCampaignStatusMessage(`Planned ${plan.planned_job_count} job(s), likely ${formatDuration(plan.estimate.likely_ms)}.`);
    } catch (err) {
      setError(readError(err));
    } finally {
      setCampaignBusy(false);
    }
  }

  async function startCampaign() {
    setCampaignBusy(true);
    setError(null);
    setCampaignStatusMessage("");
    try {
      const plan = campaignPlan ?? (await rpc<CampaignPlan>("campaigns.plan", campaignRequest()));
      if (campaignPreset === "thorough" || plan.warnings.some((warning) => ["confirm", "strong"].includes(warning.level))) {
        const warningText = plan.warnings.map((warning) => warning.message).join("\n");
        const confirmed = window.confirm(
          `Start this benchmark campaign?\n\n${plan.planned_job_count} job(s), likely ${formatDuration(plan.estimate.likely_ms)}.\n${warningText}`
        );
        if (!confirmed) {
          setCampaignStatusMessage("Campaign start cancelled.");
          return;
        }
      }
      const campaign = await rpc<BenchmarkCampaign>("campaigns.create", { plan });
      const started = await rpc<BenchmarkCampaign>("campaigns.start", { campaign_id: campaign.id });
      setActiveCampaign(started);
      setCampaigns((current) => [started, ...current.filter((item) => item.id !== started.id)]);
      setCampaignPlan(null);
      setCampaignStatusMessage(`Campaign started: ${started.title}`);
    } catch (err) {
      setError(readError(err));
    } finally {
      setCampaignBusy(false);
    }
  }

  async function refreshCampaign(campaignId: string, options: { quiet?: boolean } = {}) {
    try {
      const [campaign, nextCampaigns] = await Promise.all([
        rpc<BenchmarkCampaign>("campaigns.get", { campaign_id: campaignId }),
        rpc<BenchmarkCampaign[]>("campaigns.list", { limit: 20 })
      ]);
      setActiveCampaign(campaign);
      setCampaigns(nextCampaigns);
      if (
        shouldAutoGenerateCampaignReport(campaign) &&
        !autoReportAttempts.current.has(campaign.id) &&
        (!campaign.report_paths || Object.keys(campaign.report_paths).length === 0)
      ) {
        autoReportAttempts.current.add(campaign.id);
        void generateCampaignReport(campaign, { automatic: true, screenshots: true });
      }
    } catch (err) {
      if (!options.quiet) setError(readError(err));
    }
  }

  async function pauseCampaign(campaign: BenchmarkCampaign) {
    setCampaignBusy(true);
    setError(null);
    try {
      const updated = await rpc<BenchmarkCampaign>("campaigns.pause", { campaign_id: campaign.id });
      setActiveCampaign(updated);
      setCampaignStatusMessage("Campaign will pause before the next job starts.");
    } catch (err) {
      setError(readError(err));
    } finally {
      setCampaignBusy(false);
    }
  }

  async function cancelCampaign(campaign: BenchmarkCampaign) {
    setCampaignBusy(true);
    setError(null);
    try {
      const updated = await rpc<BenchmarkCampaign>("campaigns.cancel", { campaign_id: campaign.id });
      setActiveCampaign(updated);
      setCampaignStatusMessage("Campaign cancellation requested.");
    } catch (err) {
      setError(readError(err));
    } finally {
      setCampaignBusy(false);
    }
  }

  async function resumeCampaign(campaign: BenchmarkCampaign) {
    setCampaignBusy(true);
    setError(null);
    try {
      const updated = await rpc<BenchmarkCampaign>("campaigns.resume", { campaign_id: campaign.id });
      setActiveCampaign(updated);
      setCampaignStatusMessage("Campaign resumed.");
    } catch (err) {
      setError(readError(err));
    } finally {
      setCampaignBusy(false);
    }
  }

  async function retryCampaignJob(job: CampaignJob) {
    if (!job.id) return;
    setCampaignBusy(true);
    setError(null);
    try {
      const updated = await rpc<BenchmarkCampaign>("campaigns.retry_job", { job_id: job.id });
      setActiveCampaign(updated);
      setCampaignStatusMessage("Job queued for retry.");
    } catch (err) {
      setError(readError(err));
    } finally {
      setCampaignBusy(false);
    }
  }

  async function deleteCampaign(campaign: BenchmarkCampaign) {
    setCampaignBusy(true);
    setError(null);
    try {
      await rpc("campaigns.delete", { campaign_id: campaign.id });
      const nextCampaigns = await rpc<BenchmarkCampaign[]>("campaigns.list", { limit: 20 });
      setCampaigns(nextCampaigns);
      setActiveCampaign(nextCampaigns[0] ?? null);
      setCampaignStatusMessage("Campaign record deleted. Linked benchmark history was preserved.");
    } catch (err) {
      setError(readError(err));
    } finally {
      setCampaignBusy(false);
    }
  }

  async function chooseCampaignOutputFolder() {
    setError(null);
    try {
      const selected = await open({
        directory: true,
        multiple: false,
        title: "Choose benchmark report output folder"
      });
      if (typeof selected === "string") {
        setCampaignOutputFolder(selected);
        setCampaignPlan(null);
      }
    } catch (err) {
      setError(readError(err));
    }
  }

  async function generateCampaignReport(
    campaign: BenchmarkCampaign,
    options: { automatic?: boolean; screenshots?: boolean } = {}
  ) {
    setCampaignBusy(!options.automatic);
    setError(null);
    try {
      const data = await rpc<CampaignReportData>("campaigns.report_data", { campaign_id: campaign.id });
      const capture = options.screenshots === false
        ? { screenshots: [], failureReason: "screenshot capture was disabled for this request" }
        : await captureReportSnapshots(data);
      const result = await rpc<CampaignReportResult>("campaigns.generate_report", {
        campaign_id: campaign.id,
        output_folder: campaign.output_folder || campaignOutputFolder || undefined,
        screenshots: capture.screenshots,
        capture_failure_reason: capture.failureReason || undefined
      });
      await refreshCampaign(campaign.id, { quiet: true });
      setCampaignStatusMessage(`Report ${result.status}: ${Object.values(result.paths)[0] ?? "created"}`);
    } catch (err) {
      setError(readError(err));
    } finally {
      setCampaignBusy(false);
    }
  }

  async function captureReportSnapshots(
    data: CampaignReportData
  ): Promise<{ screenshots: Array<{ name: string; label: string; data_url: string }>; failureReason: string }> {
    setReportData(data);
    await Promise.resolve();
    if (document.fonts?.ready) {
      await document.fonts.ready;
    }
    await nextFrame();
    await nextFrame();
    const root = reportViewRef.current;
    if (!root) return { screenshots: [], failureReason: "report capture root was not mounted" };
    const nodes = Array.from(root.querySelectorAll<HTMLElement>("[data-report-snapshot]"));
    if (nodes.length === 0) {
      return { screenshots: [], failureReason: "report view mounted with no capture sections" };
    }
    const screenshots: Array<{ name: string; label: string; data_url: string }> = [];
    for (const node of nodes) {
      const name = node.dataset.reportSnapshot || `snapshot-${screenshots.length + 1}.png`;
      const label = node.dataset.reportLabel || name.replace(/^\d+-/, "").replace(/-/g, " ").replace(/\.png$/, "");
      try {
        await waitForImages(node);
        const rect = node.getBoundingClientRect();
        if (rect.width < 80 || rect.height < 40) {
          return { screenshots, failureReason: `${label} had zero or tiny layout dimensions (${Math.round(rect.width)}x${Math.round(rect.height)})` };
        }
        const canvas = await html2canvas(node, {
          backgroundColor: "#ffffff",
          logging: false,
          scale: 1.5,
          windowWidth: Math.ceil(rect.width),
          windowHeight: Math.ceil(rect.height)
        });
        const data_url = canvas.toDataURL("image/png");
        if (!isUsefulPngDataUrl(data_url)) {
          return { screenshots, failureReason: `${label} produced an invalid or tiny PNG data URL` };
        }
        screenshots.push({ name, label, data_url });
      } catch {
        return { screenshots, failureReason: `capture failed while rendering ${label}` };
      }
    }
    return { screenshots, failureReason: screenshots.length === 0 ? "no screenshots were captured" : "" };
  }

  async function openCampaignReport(campaign: BenchmarkCampaign, kind: "pdf" | "html" | "folder") {
    const path = kind === "folder" ? undefined : campaign.report_paths?.[kind];
    if (kind !== "folder" && !path) return;
    setError(null);
    try {
      await rpc("campaigns.open_report_folder", {
        campaign_id: campaign.id,
        path
      });
    } catch (err) {
      setError(readError(err));
    }
  }

  function toggleCampaignModel(model: string, selected: boolean) {
    setCampaignSelectedModels((current) =>
      selected ? dedupeStrings([...current, model]) : current.filter((item) => item !== model)
    );
    setCampaignPlan(null);
  }

  function selectAllChatCampaignModels() {
    setCampaignSelectedModels(campaignModels.filter((model) => model.capability === "chat").map((model) => model.name));
    setCampaignPlan(null);
  }

  function clearCampaignModels() {
    setCampaignSelectedModels([]);
    setCampaignPlan(null);
  }

  function toggleCampaignMode(mode: BenchmarkMode, selected: boolean) {
    setCampaignModes((current) => (selected ? dedupeBenchmarkModes([...current, mode]) : current.filter((item) => item !== mode)));
    setCampaignPlan(null);
  }

  function toggleCampaignThinking(mode: Exclude<ThinkingMode, "legacy/unrecorded">, selected: boolean) {
    setCampaignThinkingModes((current) =>
      selected ? dedupeThinkingModes([...current, mode]) : current.filter((item) => item !== mode)
    );
    setCampaignPlan(null);
  }

  function toggleCampaignVerifier(value: boolean, selected: boolean) {
    setCampaignVerifierSettings((current) => {
      const next = selected ? [...current, value] : current.filter((item) => item !== value);
      return Array.from(new Set(next)).sort((a, b) => Number(a) - Number(b));
    });
    setCampaignPlan(null);
  }

  function removePlannedCampaignJob(job: CampaignJob) {
    if (!campaignPlan) return;
    setCampaignPlan({
      ...campaignPlan,
      planned_jobs: campaignPlan.planned_jobs.filter((item) => item.key !== job.key),
      planned_job_count: Math.max(0, campaignPlan.planned_job_count - 1)
    });
  }

  async function saveSettings() {
    const requestedModel = modelDraft.trim() || "llama3.2";
    if (ollama?.reachable && modelChoices.length === 0) {
      setError("No installed chat-capable Ollama model is available for conversations.");
      return;
    }
    if (modelChoices.length > 0 && !isInstalledModelTag(requestedModel, modelChoices)) {
      setError(`Choose an installed conversation model before saving settings. ${readableModelLabel(requestedModel, { installed: false })} is historical only.`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const next = await rpc<Settings>("settings.set", {
        values: {
          default_model: modelChoices.length > 0 ? installedModelTag(requestedModel, modelChoices) : requestedModel,
          vision_backend: visionBackendDraft
        }
      });
      setSettings(next);
      setModelDraft(selectInstalledModelOrFallback(String(next.default_model ?? "llama3.2"), modelChoices));
      setVisionBackendDraft(normalizeVisionBackend(next.vision_backend));
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function createSession() {
    if (ollama?.reachable && modelChoices.length === 0) {
      setError("No installed chat-capable Ollama model is available for conversations.");
      return;
    }
    const requestedModel = modelDraft.trim() || String(settings.default_model || "llama3.2");
    const sessionModel = modelChoices.length > 0
      ? selectInstalledModelOrFallback(requestedModel, modelChoices)
      : requestedModel;
    setBusy(true);
    setError(null);
    try {
      const session = await rpc<Session>("sessions.create", {
        model: sessionModel
      });
      const nextSessions = await rpc<Session[]>("sessions.list");
      setSessions(nextSessions);
      setSelectedSessionId(session.id);
      setActiveView("chat");
      setPendingSources([]);
      setConversationContext([]);
      setSelectedRagDocumentId("");
      setUseRag(false);
      setRetrievedChunks([]);
      setRetrievedSnippets([]);
      setGrounding(null);
      setLastChatAnalysis(null);
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function changeActiveSessionModel(model: string) {
    if (!selectedSessionId) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await rpc<Session>("sessions.update", {
        session_id: selectedSessionId,
        updates: { model }
      });
      setSessions((current) => current.map((session) => (session.id === updated.id ? updated : session)));
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function deleteSession(sessionId: string) {
    setBusy(true);
    setError(null);
    try {
      await rpc("sessions.delete", { session_id: sessionId });
      const nextSessions = await rpc<Session[]>("sessions.list");
      setSessions(nextSessions);
      if (selectedSessionId === sessionId) {
        setSelectedSessionId(nextSessions[0]?.id ?? null);
      }
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function loadMessages(sessionId: string) {
    setError(null);
    try {
      setMessages(await rpc<Message[]>("sessions.messages", { session_id: sessionId }));
    } catch (err) {
      setError(readError(err));
    }
  }

  async function loadConversationContext(sessionId: string) {
    try {
      setConversationContext(await conversationSources(sessionId));
    } catch (err) {
      setError(readError(err));
    }
  }

  async function chooseAttachmentFiles() {
    setError(null);
    try {
      const selected = await open({
        multiple: true,
        filters: [
          {
            name: "Sources",
            extensions: ["pdf", "txt", "md", "png", "jpg", "jpeg", "webp"]
          }
        ]
      });
      const paths = Array.isArray(selected) ? selected : typeof selected === "string" ? [selected] : [];
      await importSourcePaths(paths, "session", "chat");
    } catch (err) {
      setError(readError(err));
    }
  }

  async function chooseLibrarySources() {
    setError(null);
    try {
      const selected = await open({
        multiple: true,
        filters: [
          {
            name: "Sources",
            extensions: ["pdf", "txt", "md", "png", "jpg", "jpeg", "webp"]
          }
        ]
      });
      const paths = Array.isArray(selected) ? selected : typeof selected === "string" ? [selected] : [];
      await importSourcePaths(paths, "library", "library");
    } catch (err) {
      setError(readError(err));
    }
  }

  async function importSourcePaths(
    paths: string[],
    scope: "library" | "session",
    target: "library" | "chat",
    sourceKind = "file"
  ) {
    const validPaths = dedupeStrings(paths).filter(isSupportedSourcePath);
    const unsupported = dedupeStrings(paths).filter((path) => !isSupportedSourcePath(path));
    if (validPaths.length === 0) {
      if (unsupported.length > 0) setError(unsupported.map((path) => `${fileName(path)}: unsupported file type`).join("; "));
      return;
    }
    const documentPaths = validPaths.filter(isSupportedDocumentPath);
    const otherPaths = validPaths.filter((path) => !isSupportedDocumentPath(path));
    setBusy(true);
    setError(null);
    let importStage: "images" | "jobs" = "images";
    try {
      let imported: SourceSummary[] = [];
      let failures: string[] = [];
      if (otherPaths.length > 0) {
        const result = await importSources({
          paths: otherPaths,
          scope,
          index: true,
          sourceKind
        });
        imported = sourceSummariesFromImportResult(result);
        failures = failedSourceImports(result).map((item) => `${item.name}: ${item.error}`);
      }
      if (documentPaths.length > 0) {
        importStage = "jobs";
        await importJobs.submitJobs(documentPaths, scope, target);
      }
      if (target === "chat") {
        appendPendingSources(imported);
        setActiveView("chat");
      } else {
        setActiveView("sources");
      }
      if (otherPaths.length > 0) await refreshSources();
      const unsupportedErrors = unsupported.map((path) => `${fileName(path)}: unsupported file type`);
      if (failures.length > 0 || unsupportedErrors.length > 0) {
        setError([...failures, ...unsupportedErrors].join("; "));
      }
    } catch (err) {
      setError(importStage === "jobs" ? jobSubmissionFailureCopy() : readError(err));
    } finally {
      setBusy(false);
    }
  }

  function appendPendingSources(nextSources: SourceSummary[]) {
    setPendingSources((current) => {
      const combined = [...current];
      for (const source of nextSources) {
        if (combined.some((item) => sameSource(item, source))) continue;
        combined.push(source);
      }
      return combined.slice(0, 8);
    });
  }

  function removePendingSource(source: SourceSummary) {
    setPendingSources((current) => current.filter((item) => !sameSource(item, source)));
  }

  async function removeSourceFromConversation(source: SourceSummary) {
    if (!selectedSessionId) return;
    setBusy(true);
    setError(null);
    try {
      const nextContext = await removeConversationSource({
        sessionId: selectedSessionId,
        backendKind: source.backend_kind,
        sourceId: source.id
      });
      setConversationContext(nextContext);
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  function addExistingSourceToChat(source: SourceSummary) {
    appendPendingSources([source]);
    setActiveView("chat");
  }

  async function promotePendingSource(source: SourceSummary, index: boolean) {
    setBusy(true);
    setError(null);
    try {
      await promoteSource({
        backendKind: source.backend_kind,
        sourceId: source.id,
        index
      });
      setPendingSources((current) =>
        current.map((item) => (sameSource(item, source) ? { ...item, scope: "library" } : item))
      );
      await refreshSources();
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function deleteUnifiedSource(source: SourceSummary) {
    const confirmed = window.confirm(deleteSourceConfirmCopy(source.display_name));
    if (!confirmed) return;
    setBusy(true);
    setError(null);
    setSourceNotice(null);
    try {
      const result = await deleteSource(source.backend_kind, source.id);
      setSourceNotice(deleteSourceResultCopy(result));
      setPendingSources((current) => current.filter((item) => !sameSource(item, source)));
      await refreshSources();
    } catch (err) {
      // Fixed-copy mapping only; raw backend error strings are never rendered.
      setError(deleteSourceErrorCopy(err));
    } finally {
      setBusy(false);
    }
  }

  async function refreshStorage() {
    setStorageLoading(true);
    setStorageError(null);
    try {
      const [status, preview] = await Promise.all([getStorageStatus(), getCleanupPreview()]);
      setStorageStatus(status);
      setCleanupPreview(preview);
    } catch (err) {
      setStorageError(storageErrorCopy(err));
    } finally {
      setStorageLoading(false);
    }
  }

  async function runStorageCleanup() {
    if (!cleanupPreview) return;
    const confirmed = window.confirm(cleanupConfirmCopy(cleanupPreview));
    if (!confirmed) return;
    setStorageCleaning(true);
    setStorageError(null);
    try {
      const result = await runCleanup();
      setLastCleanup(result);
      await refreshStorage();
    } catch (err) {
      setStorageError(storageErrorCopy(err));
    } finally {
      setStorageCleaning(false);
    }
  }

  async function importDocumentPath(path: string) {
    if (!path) return;
    const unsupported = unsupportedDocumentReason(path);
    if (unsupported) {
      setError(unsupported);
      return;
    }
    setBusy(true);
    setError(null);
    setLastIndexResult(null);
    try {
      await importJobs.submitJobs([path], "library", "library");
      setLastOcrResult(null);
      setImportPath("");
    } catch {
      setError(jobSubmissionFailureCopy());
    } finally {
      setBusy(false);
    }
  }

  async function importDocument(event: FormEvent) {
    event.preventDefault();
    await importDocumentPath(importPath.trim());
  }

  async function chooseDocument() {
    setError(null);
    try {
      const selected = await open({
        multiple: false,
        filters: [
          {
            name: "Documents",
            extensions: ["txt", "md", "pdf"]
          }
        ]
      });
      if (typeof selected !== "string") return;
      setImportPath(selected);
      await importDocumentPath(selected);
    } catch (err) {
      setError(readError(err));
    }
  }

  async function routeDroppedPaths(paths: string[]) {
    const uniquePaths = dedupeStrings(paths).slice(0, 12);
    const target = activeView === "chat" ? "chat" : "library";
    await importSourcePaths(uniquePaths, target === "chat" ? "session" : "library", target);
  }

  async function chooseChatImages() {
    await chooseImages("chat");
  }

  async function chooseLibraryImages() {
    await chooseImages("library");
  }

  async function chooseImages(target: "chat" | "library") {
    setError(null);
    try {
      const selected = await open({
        multiple: true,
        filters: [
          {
            name: "Images",
            extensions: ["png", "jpg", "jpeg", "webp"]
          }
        ]
      });
      const paths = Array.isArray(selected) ? selected : typeof selected === "string" ? [selected] : [];
      await importImagePaths(paths, target);
    } catch (err) {
      setError(readError(err));
    }
  }

  async function importImagePaths(paths: string[], target: "chat" | "library") {
    const validPaths = paths.filter(isSupportedImagePath);
    if (validPaths.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const result = await importArtifactPaths(validPaths, "file");
      await refreshArtifacts();
      if (target === "chat") {
        appendPendingArtifacts(result.imported);
        setActiveView("chat");
      } else {
        setSelectedArtifactId(result.imported[0]?.id ?? selectedArtifactId);
        setActiveView("sources");
      }
      if (result.failed.length > 0) {
        setError(result.failed.map((item) => `${fileName(item.path)}: ${item.error}`).join("; "));
      }
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function importCaptureToArtifacts(path: string, sourceKind: ArtifactRecord["source_kind"], target: "chat" | "library") {
    setBusy(true);
    setError(null);
    try {
      const artifact = await importArtifactPath(path, sourceKind);
      await refreshArtifacts();
      if (target === "chat") {
        appendPendingArtifacts([artifact]);
        setActiveView("chat");
      } else {
        setSelectedArtifactId(artifact.id);
        setActiveView("sources");
      }
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function pasteImageToChat() {
    try {
      const capture = await importClipboardImage();
      await importSourcePaths([capture.path], "session", "chat", capture.source_kind);
    } catch (err) {
      setError(readError(err));
    }
  }

  async function captureScreenToChat() {
    try {
      const capture = await captureFullScreen();
      await importSourcePaths([capture.path], "session", "chat", capture.source_kind);
    } catch (err) {
      setError(readError(err));
    }
  }

  async function pasteImageToLibrary() {
    try {
      const capture = await importClipboardImage();
      await importCaptureToArtifacts(capture.path, capture.source_kind, "library");
    } catch (err) {
      setError(readError(err));
    }
  }

  async function captureFullScreenToLibrary() {
    try {
      const capture = await captureFullScreen();
      await importCaptureToArtifacts(capture.path, capture.source_kind, "library");
    } catch (err) {
      setError(readError(err));
    }
  }

  async function captureRegionToLibrary() {
    try {
      const capture = await captureRegion(captureRegionDraft);
      await importCaptureToArtifacts(capture.path, capture.source_kind, "library");
    } catch (err) {
      setError(readError(err));
    }
  }

  function appendPendingArtifacts(nextArtifacts: ArtifactRecord[]) {
    appendPendingSources(nextArtifacts.map(artifactToSource));
  }

  function removePendingArtifact(artifactId: string) {
    setPendingSources((current) => current.filter((source) => !(source.backend_kind === "artifact" && source.id === artifactId)));
  }

  async function runOcr(documentId: string) {
    setBusy(true);
    setError(null);
    setLastOcrResult(null);
    try {
      const result = await rpc<OCRResult>("documents.ocr", { document_id: documentId });
      setLastOcrResult(result);
      setOcrStatus(result.ocr_status);
      if (!result.ocr_status.available || !result.index) {
        setError(result.stats.warning || result.document.ocr_error || result.ocr_status.message);
      }
      await refreshDocuments();
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function chooseLegacyFolder() {
    setError(null);
    try {
      const selected = await open({
        directory: true,
        multiple: false
      });
      if (typeof selected !== "string") return;
      setLegacyPath(selected);
    } catch (err) {
      setError(readError(err));
    }
  }

  async function importLegacyFolder(event: FormEvent) {
    event.preventDefault();
    const folder = legacyPath.trim();
    if (!folder) return;
    setBusy(true);
    setError(null);
    setLegacyReport(null);
    try {
      const report = await rpc<LegacyImportReport>("legacy.import", { folder });
      setLegacyReport(report);
      await Promise.all([refreshDocuments(), refreshSessions()]);
      const nextSettings = await rpc<Settings>("settings.get");
      setSettings(nextSettings);
      setModelDraft(String(nextSettings.default_model ?? "llama3.2"));
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function refreshSessions() {
    const nextSessions = await rpc<Session[]>("sessions.list");
    setSessions(nextSessions);
    setSelectedSessionId((current) => current ?? nextSessions[0]?.id ?? null);
  }

  async function deleteDocument(documentId: string) {
    setBusy(true);
    setError(null);
    try {
      await rpc("documents.delete", { document_id: documentId });
      await refreshDocuments();
      if (searchQuery.trim()) {
        await testRetrieval();
      }
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function reindexDocument(documentId: string) {
    setBusy(true);
    setError(null);
    setLastIndexResult(null);
    try {
      const result = await rpc<RAGIndexResult>("documents.reindex", { document_id: documentId });
      setLastIndexResult(result);
      await refreshDocuments();
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function testRetrieval(event?: FormEvent) {
    event?.preventDefault();
    const query = searchQuery.trim();
    if (!query) return;
    setBusy(true);
    setError(null);
    try {
      setSearchResults(await rpc<RAGSearchResult[]>("rag.search", { query, limit: 5 }));
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function analyzeSelectedArtifact() {
    if (!selectedArtifactId) return;
    const activeVisionBackend = normalizeVisionBackend(visionBackendDraft || settings.vision_backend);
    if (
      activeVisionBackend === "ollama" &&
      (imageAnalysisMode === "vision_only" || imageAnalysisMode === "combined") &&
      !imageVisionModel.trim()
    ) {
      setError("Choose a confirmed local vision model, or switch to OCR only.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const analysis = await analyzeArtifact({
        artifactId: selectedArtifactId,
        mode: imageAnalysisMode,
        visionBackend: activeVisionBackend,
        question: imageAnalysisQuestion,
        visionModel: activeVisionBackend === "florence2" || activeVisionBackend === "ocr_only" ? "" : imageVisionModel,
        requestId: makeRequestId("artifact-analysis")
      });
      setLastArtifactAnalysis(analysis);
      await refreshSelectedArtifact(selectedArtifactId);
      await refreshArtifacts();
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function deleteSelectedArtifact(artifactId: string) {
    const confirmed = window.confirm("Delete this image and its derived files from the local profile?");
    if (!confirmed) return;
    setBusy(true);
    setError(null);
    try {
      await deleteArtifact(artifactId);
      setPendingSources((current) => current.filter((source) => !(source.backend_kind === "artifact" && source.id === artifactId)));
      await refreshArtifacts();
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function indexArtifactText(artifactId: string, derivationId: string) {
    setBusy(true);
    setError(null);
    try {
      await indexArtifactDerivation(artifactId, derivationId);
      await Promise.all([refreshDocuments(), refreshSelectedArtifact(artifactId)]);
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function unindexArtifactText(artifactId: string) {
    setBusy(true);
    setError(null);
    try {
      await unindexArtifact(artifactId);
      await Promise.all([refreshDocuments(), refreshSelectedArtifact(artifactId)]);
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function copyText(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopyStatus("Copied text.");
    } catch (err) {
      setError(readError(err));
    }
  }

  async function refreshCapabilities() {
    setBusy(true);
    setError(null);
    try {
      const capabilities = await refreshModelCapabilities();
      setModelCapabilities(capabilities);
      setDiagnostics((current) =>
        current
          ? {
              ...current,
              image_vision: current.image_vision
                ? { ...current.image_vision, model_capabilities: capabilities }
                : current.image_vision
            }
          : current
      );
      const defaultVision = visionCapableModels(capabilities)[0]?.model ?? "";
      setChatVisionModel((current) => current || defaultVision);
      setImageVisionModel((current) => current || defaultVision);
      setImageEvalModel((current) => current || defaultVision);
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function runImageBenchmark() {
    if (imageEvalMode !== "ocr_only" && !imageEvalModel.trim()) {
      setError("Choose a confirmed local vision model for this image benchmark mode.");
      return;
    }
    setBusy(true);
    setError(null);
    setImageCopyStatus("");
    try {
      const run = await runImageEval(imageEvalMode, imageEvalMode === "ocr_only" ? "" : imageEvalModel.trim());
      setImageEvalResult(run);
      setImageEvalHistory(await listImageEvalHistory(20));
    } catch (err) {
      setError(readError(err));
    } finally {
      setBusy(false);
    }
  }

  async function copyImageBenchmarkSummary() {
    const summary = imageBenchmarkSummaryMarkdown(imageEvalResult ? [imageEvalResult] : imageEvalHistory);
    if (!summary.trim()) return;
    try {
      await navigator.clipboard.writeText(summary);
      setImageCopyStatus("Copied image benchmark summary.");
    } catch (err) {
      setError(readError(err));
    }
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    const message = draft.trim();
    const attachedSources = pendingSources;
    const attachedArtifacts = attachedSources.filter((source) => source.backend_kind === "artifact");
    const attachedDocuments = attachedSources.filter((source) => source.backend_kind === "document");
    if (!message && attachedSources.length === 0) return;
    if (ollama?.reachable && modelChoices.length === 0) {
      setError("No installed chat-capable Ollama model is available for conversations.");
      return;
    }
    const requestedModel = String(selectedSession?.model || modelDraft.trim() || settings.default_model || "llama3.2");
    if (modelChoices.length > 0 && !isInstalledModelTag(requestedModel, modelChoices)) {
      setError(`Choose an installed conversation model before sending. ${readableModelLabel(requestedModel, { installed: false })} is historical only.`);
      return;
    }
    const activeConversationModel = modelChoices.length > 0 ? installedModelTag(requestedModel, modelChoices) : requestedModel;
    if (
      normalizeVisionBackend(visionBackendDraft || settings.vision_backend) === "ollama" &&
      attachedArtifacts.length > 0 &&
      (chatMultimodalMode === "vision_only" || chatMultimodalMode === "combined") &&
      !chatVisionModel.trim()
    ) {
      setError("Choose a confirmed local vision model, or switch image mode to OCR only.");
      return;
    }
    setBusy(true);
    setError(null);
    setDraft("");
    setPendingUserContent(message || "Attachment");
    setLastChatAnalysis(null);
    setRetrievedChunks([]);
    setRetrievedSnippets([]);
    setDocumentEvidence([]);
    setGrounding(null);
    try {
      const params: Record<string, unknown> = {
        message,
        session_id: selectedSessionId,
        model: activeConversationModel,
        use_rag: useRag || attachedDocuments.length > 0,
        verify_rag: useRag && ragPreset !== "potato" && verifyRag,
        answer_style: answerStyle,
        rag_preset: ragPreset
      };
      if (useRag && selectedRagDocumentId) {
        params.document_ids = [selectedRagDocumentId];
      }
      if (attachedDocuments.length > 0) {
        params.attachment_document_ids = attachedDocuments.map((source) => source.id);
      }
      if (attachedArtifacts.length > 0) {
        const activeVisionBackend = normalizeVisionBackend(visionBackendDraft || settings.vision_backend);
        params.artifact_ids = attachedArtifacts.map((source) => source.id);
        params.multimodal_mode = chatMultimodalMode;
        params.vision_backend = activeVisionBackend;
        params.vision_model = chatMultimodalMode === "ocr_only" || activeVisionBackend === "florence2" || activeVisionBackend === "ocr_only" ? "" : chatVisionModel.trim();
        params.analysis_request_id = makeRequestId("chat-image");
      }
      const result = await rpc<ChatResult>("chat.send", params);
      setSelectedSessionId(result.session.id);
      setMessages(result.messages);
      setRetrievedChunks(result.retrieved_chunks ?? []);
      setRetrievedSnippets(result.retrieved_snippets ?? []);
      setDocumentEvidence(result.document_evidence ?? []);
      setGrounding(result.grounding ?? null);
      setLastChatAnalysis(result.artifact_analysis ?? null);
      await loadConversationContext(result.session.id);
      if (attachedSources.length > 0) {
        setPendingSources([]);
        await refreshSources();
      }
      setSessions(await rpc<Session[]>("sessions.list"));
    } catch (err) {
      setDraft(message);
      setError(readError(err));
      if (selectedSessionId) {
        await loadMessages(selectedSessionId);
      }
    } finally {
      setPendingUserContent("");
      setBusy(false);
    }
  }

  const hasRuntime = Boolean(ollama?.reachable);

  return (
    <main className="flex h-screen min-h-[620px] bg-paper text-ink">
      <AppSidebar
        activeView={activeView}
        appStatus={appStatus}
        busy={busy}
        modelChoices={modelChoices}
        modelDraft={modelDraft}
        ollama={ollama}
        selectedSessionId={selectedSessionId}
        sessions={sessions}
        settings={settings}
        settingsOpen={settingsOpen}
        visionBackendDraft={visionBackendDraft}
        onCreateSession={createSession}
        onRefreshOllama={refreshOllama}
        onSaveSettings={saveSettings}
        onSelectSession={(sessionId) => {
          setSelectedSessionId(sessionId);
          setActiveView("chat");
        }}
        onSetActiveView={setActiveView}
        onSetModelDraft={setModelDraft}
        onSetVisionBackendDraft={setVisionBackendDraft}
        onToggleSettings={() => setSettingsOpen((open) => !open)}
      />

      <section className="flex min-w-0 flex-1 flex-col">
        <BackendDegradedBanner
          degraded={backendDegraded}
          retrying={backendRetrying}
          onRetry={retryDegradedBackend}
        />
        {!showReadiness && (
          <JobsPanel jobs={importJobs.jobs} onCancel={importJobs.cancelJob} onRemove={importJobs.removeJob} />
        )}
        {showReadiness ? (
          <ReadinessPanel
            rows={readinessRows({
              checking: readinessChecking,
              backendDegraded,
              ollama,
              ragHealth,
              ocrStatus
            })}
            checking={readinessChecking}
            onRecheck={() => void recheckReadiness()}
            onContinue={() => setShowReadiness(false)}
          />
        ) : activeView === "chat" ? (
          <ChatWorkspace
            busy={busy}
            draft={draft}
            error={error}
            hasRuntime={hasRuntime}
            loadState={loadState}
            documents={documents}
            messages={messages}
            retrievedChunks={retrievedChunks}
            retrievedSnippets={retrievedSnippets}
            documentEvidence={documentEvidence}
            grounding={grounding}
            pendingSources={pendingSources}
            pendingUserContent={pendingUserContent}
            progressEvent={chatProgress.current}
            progressFallbackLabel={chatProgress.fallbackLabel}
            progressNow={chatProgress.now}
            conversationContext={conversationContext}
            sources={sources}
            modelChoices={modelChoices}
            multimodalMode={chatMultimodalMode}
            visionBackend={normalizeVisionBackend(visionBackendDraft || settings.vision_backend)}
            visionModel={chatVisionModel}
            visionModels={confirmedVisionModels}
            artifactAnalysis={lastChatAnalysis}
            selectedRagDocumentId={selectedRagDocumentId}
            selectedSession={selectedSession}
            settings={settings}
            answerStyle={answerStyle}
            ragPreset={ragPreset}
            useRag={useRag}
            verifyRag={verifyRag}
            onDeleteSession={deleteSession}
            onRetry={bootstrap}
            onSend={sendMessage}
            onAddExistingSource={addExistingSourceToChat}
            onAttachFiles={chooseAttachmentFiles}
            onCaptureScreen={captureScreenToChat}
            onPasteImage={pasteImageToChat}
            onPromoteSource={promotePendingSource}
            onRemoveConversationSource={removeSourceFromConversation}
            onRemovePendingSource={removePendingSource}
            onSetMultimodalMode={setChatMultimodalMode}
            onSetDraft={setDraft}
            onSetSessionModel={changeActiveSessionModel}
            onSetVisionModel={setChatVisionModel}
            onSetAnswerStyle={setAnswerStyle}
            onSetSelectedRagDocumentId={setSelectedRagDocumentId}
            onSetRagPreset={(value) => {
              setRagPreset(value);
              if (value === "potato") {
                setUseRag(true);
                setVerifyRag(false);
                setAnswerStyle("evidence_only");
              }
            }}
            onSetUseRag={setUseRag}
            onSetVerifyRag={setVerifyRag}
          />
        ) : activeView === "sources" ? (
          <SourcesPage
            busy={busy}
            error={error}
            notice={sourceNotice}
            filter={sourceFilter}
            sources={sources}
            onAddSources={chooseLibrarySources}
            onDelete={deleteUnifiedSource}
            onFilter={(filter) => {
              setSourceFilter(filter);
              void refreshSources(filter);
            }}
            onPromote={promotePendingSource}
            onRefresh={() => refreshSources()}
          />
        ) : activeView === "storage" ? (
          <StoragePanel
            status={storageStatus}
            preview={cleanupPreview}
            lastCleanup={lastCleanup}
            loading={storageLoading}
            cleaning={storageCleaning}
            error={storageError}
            onRefresh={() => void refreshStorage()}
            onCleanup={() => void runStorageCleanup()}
          />
        ) : (
          <DiagnosticsWorkspace
            benchmarkModel={benchmarkModel}
            benchmarkResult={benchmarkResult}
            benchmarkVerify={benchmarkVerify}
            busy={busy}
            campaignAutoReport={campaignAutoReport}
            campaignBusy={campaignBusy}
            campaignModels={campaignModels}
            campaignModes={campaignModes}
            campaignOutputFolder={campaignOutputFolder}
            campaignPlan={campaignPlan}
            campaignPreset={campaignPreset}
            campaignRepeats={campaignRepeats}
            campaignSelectedModels={campaignSelectedModels}
            campaignStatusMessage={campaignStatusMessage}
            campaignThinkingModes={campaignThinkingModes}
            campaignTitle={campaignTitle}
            campaignVerifierSettings={campaignVerifierSettings}
            campaigns={campaigns}
            copyStatus={copyStatus}
            comparison={benchmarkComparison}
            diagnostics={diagnostics}
            progressEvents={chatProgress.history}
            error={error}
            evalHistory={evalHistory}
            evalSuite={evalSuite}
            imageCopyStatus={imageCopyStatus}
            imageEvalHistory={imageEvalHistory}
            imageEvalMode={imageEvalMode}
            imageEvalModel={imageEvalModel}
            imageEvalResult={imageEvalResult}
            imageEvalSuite={imageEvalSuite}
            selectedVisionModel={imageVisionModel || chatVisionModel}
            visionModels={confirmedVisionModels}
            activeCampaign={activeCampaign}
            benchmarkMode={benchmarkMode}
            benchmarkRepeats={benchmarkRepeats}
            benchmarkThinking={benchmarkThinking}
            onCancelCampaign={cancelCampaign}
            onClearHistory={clearBenchmarkHistory}
            onCopySummary={copyBenchmarkSummary}
            onClearCampaignModels={clearCampaignModels}
            onChooseCampaignOutputFolder={chooseCampaignOutputFolder}
            onDeleteCampaign={deleteCampaign}
            onGenerateCampaignReport={(campaign) => generateCampaignReport(campaign, { screenshots: true })}
            onOpenCampaignReport={openCampaignReport}
            onPauseCampaign={pauseCampaign}
            onRefresh={refreshDiagnostics}
            onRefreshCampaign={(campaignId) => refreshCampaign(campaignId)}
            onRemovePlannedCampaignJob={removePlannedCampaignJob}
            onResumeCampaign={resumeCampaign}
            onRetryCampaignJob={retryCampaignJob}
            onReviewCampaignPlan={reviewCampaignPlan}
            onRunBenchmark={runBenchmark}
            onSelectAllChatCampaignModels={selectAllChatCampaignModels}
            onSetBenchmarkMode={setBenchmarkMode}
            onSetBenchmarkModel={setBenchmarkModel}
            onSetBenchmarkRepeats={setBenchmarkRepeats}
            onSetBenchmarkThinking={setBenchmarkThinking}
            onSetBenchmarkVerify={setBenchmarkVerify}
            onCopyImageSummary={copyImageBenchmarkSummary}
            onRefreshModelCapabilities={refreshCapabilities}
            onRunImageBenchmark={runImageBenchmark}
            onSetImageEvalMode={setImageEvalMode}
            onSetImageEvalModel={setImageEvalModel}
            onSetCampaignAutoReport={setCampaignAutoReport}
            onSetCampaignModes={setCampaignModes}
            onSetCampaignOutputFolder={setCampaignOutputFolder}
            onSetCampaignPreset={(value) => {
              setCampaignPreset(value);
              setCampaignTitle(defaultCampaignTitle(value));
              if (value === "quick") {
                setCampaignModes(["end_to_end"]);
                setCampaignThinkingModes(["off"]);
                setCampaignVerifierSettings([false]);
                setCampaignRepeats(1);
              }
              if (value === "standard") {
                setCampaignModes(["retrieval_only", "oracle_generation", "end_to_end"]);
                setCampaignThinkingModes(["off"]);
                setCampaignVerifierSettings([false]);
                setCampaignRepeats(1);
              }
              setCampaignPlan(null);
            }}
            onSetCampaignRepeats={setCampaignRepeats}
            onSetCampaignSelectedModels={setCampaignSelectedModels}
            onSetCampaignThinkingModes={setCampaignThinkingModes}
            onSetCampaignTitle={setCampaignTitle}
            onSetCampaignVerifierSettings={setCampaignVerifierSettings}
            onStartCampaign={startCampaign}
            onToggleCampaignMode={toggleCampaignMode}
            onToggleCampaignModel={toggleCampaignModel}
            onToggleCampaignThinking={toggleCampaignThinking}
            onToggleCampaignVerifier={toggleCampaignVerifier}
            onOpenReadiness={() => setShowReadiness(true)}
          />
        )}
      </section>
      <div
        aria-hidden="true"
        className="pointer-events-none fixed left-[-12000px] top-0 w-[1040px] bg-white"
        ref={reportViewRef}
      >
        {reportData && <BenchmarkReportView data={reportData} />}
      </div>
    </main>
  );
}

function ChatWorkspace(props: {
  busy: boolean;
  draft: string;
  error: string | null;
  hasRuntime: boolean;
  loadState: LoadState;
  documents: DocumentRecord[];
  messages: Message[];
  retrievedChunks: RAGSearchResult[];
  retrievedSnippets: RAGSnippet[];
  documentEvidence: DocumentEvidenceDiagnostic[];
  grounding: RAGGroundingReport | null;
  pendingSources: SourceSummary[];
  pendingUserContent: string;
  progressEvent: OperationProgressEvent | null;
  progressFallbackLabel: string | null;
  progressNow: number;
  conversationContext: SourceSummary[];
  sources: SourceSummary[];
  modelChoices: string[];
  multimodalMode: MultimodalMode;
  visionBackend: VisionBackend;
  visionModel: string;
  visionModels: ModelCapability[];
  artifactAnalysis: ArtifactAnalysisRun | null;
  selectedRagDocumentId: string;
  selectedSession: Session | null;
  settings: Settings;
  answerStyle: AnswerStyle;
  ragPreset: RagPreset;
  useRag: boolean;
  verifyRag: boolean;
  onAddExistingSource: (source: SourceSummary) => void;
  onAttachFiles: () => void;
  onCaptureScreen: () => void;
  onDeleteSession: (sessionId: string) => void;
  onPasteImage: () => void;
  onPromoteSource: (source: SourceSummary, index: boolean) => void;
  onRemoveConversationSource: (source: SourceSummary) => void;
  onRemovePendingSource: (source: SourceSummary) => void;
  onRetry: () => void;
  onSend: (event: FormEvent) => void;
  onSetAnswerStyle: (value: AnswerStyle) => void;
  onSetDraft: (value: string) => void;
  onSetMultimodalMode: (value: MultimodalMode) => void;
  onSetRagPreset: (value: RagPreset) => void;
  onSetSelectedRagDocumentId: (value: string) => void;
  onSetSessionModel: (value: string) => void;
  onSetUseRag: (value: boolean) => void;
  onSetVerifyRag: (value: boolean) => void;
  onSetVisionModel: (value: string) => void;
}) {
  const latestAssistantMessageId = [...props.messages]
    .reverse()
    .find((message) => message.role === "assistant")?.id;
  const latestMessageId = props.messages[props.messages.length - 1]?.id;
  const showVision = props.pendingSources.some((source) => source.backend_kind === "artifact") ||
    props.conversationContext.some((source) => source.backend_kind === "artifact") ||
    Boolean(props.artifactAnalysis);

  return (
    <>
      <ChatHeader
        answerStyle={props.answerStyle}
        busy={props.busy}
        documents={props.documents}
        modelChoices={props.modelChoices}
        multimodalMode={props.multimodalMode}
        visionBackend={props.visionBackend}
        ragPreset={props.ragPreset}
        selectedRagDocumentId={props.selectedRagDocumentId}
        selectedSession={props.selectedSession}
        settings={props.settings}
        showVision={showVision}
        useRag={props.useRag}
        verifyRag={props.verifyRag}
        visionModel={props.visionModel}
        visionModels={props.visionModels}
        onDeleteSession={props.onDeleteSession}
        onSetAnswerStyle={props.onSetAnswerStyle}
        onSetRagPreset={props.onSetRagPreset}
        onSetSelectedRagDocumentId={props.onSetSelectedRagDocumentId}
        onSetSessionModel={props.onSetSessionModel}
        onSetUseRag={props.onSetUseRag}
        onSetVerifyRag={props.onSetVerifyRag}
      />

      <ErrorBanner error={props.error} />

      {props.loadState === "error" ? (
        <div className="flex flex-1 items-center justify-center px-6">
          <button
            className="rounded-md bg-moss px-4 py-2 text-sm font-medium text-white"
            onClick={props.onRetry}
            type="button"
          >
            Retry startup
          </button>
        </div>
      ) : (
        <>
          <div className="scrollbar-thin flex-1 overflow-auto px-5 py-5">
            {props.messages.length === 0 && !props.pendingUserContent && !props.busy ? (
              <div className="flex h-full items-center justify-center text-center">
                <div>
                  <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-md bg-gold/20 text-gold">
                    <Bot size={24} aria-hidden="true" />
                  </div>
                  <p className="text-base font-medium">Ready</p>
                  <p className="mt-1 text-sm text-ink/55">
                    {props.hasRuntime ? "Ollama is reachable." : "Connect Ollama to send a local chat."}
                  </p>
                </div>
              </div>
            ) : (
              <div className="mx-auto flex max-w-3xl flex-col gap-3">
                {props.messages.map((message) => (
                  <div className="flex flex-col gap-2" key={message.id}>
                    <MessageBubble message={message} />
                    {message.role === "assistant" && message.metadata?.operation_trace && (
                      <OperationTrace
                        answer={message.content}
                        hasEvidence={message.id === latestAssistantMessageId && Boolean(
                          props.retrievedChunks.length || props.documentEvidence.length || props.artifactAnalysis
                        )}
                        trace={message.metadata.operation_trace}
                      />
                    )}
                    {message.id === latestAssistantMessageId && props.retrievedChunks.length > 0 && (
                      <RetrievedSources
                        chunks={props.retrievedChunks}
                        grounding={props.grounding}
                        snippets={props.retrievedSnippets}
                        detailed
                      />
                    )}
                    {message.id === latestAssistantMessageId && props.documentEvidence.length > 0 && (
                      <DocumentEvidenceCard evidence={props.documentEvidence} />
                    )}
                    {message.id === latestMessageId && props.artifactAnalysis && (
                      <ArtifactAnalysisCard analysis={props.artifactAnalysis} />
                    )}
                  </div>
                ))}
                {props.pendingUserContent && (
                  <div className="flex justify-end">
                    <div className="max-w-[82%] whitespace-pre-wrap rounded-md bg-moss px-3 py-2 text-sm leading-6 text-white">
                      {props.pendingUserContent}
                    </div>
                  </div>
                )}
                {props.busy && (
                  <ChatProgressBar
                    event={props.progressEvent}
                    fallbackLabel={props.progressFallbackLabel}
                    now={props.progressNow}
                  />
                )}
              </div>
            )}
          </div>

          <ComposerAttachments
            attachments={props.pendingSources}
            conversationContext={props.conversationContext}
            busy={props.busy}
            librarySources={props.sources}
            mode={props.multimodalMode}
            visionModel={props.visionModel}
            visionModels={props.visionModels}
            onAddSource={props.onAddExistingSource}
            onAttachFiles={props.onAttachFiles}
            onCaptureScreen={props.onCaptureScreen}
            onPasteImage={props.onPasteImage}
            onPromote={props.onPromoteSource}
            onRemoveConversationSource={props.onRemoveConversationSource}
            onRemove={props.onRemovePendingSource}
            onSetMode={props.onSetMultimodalMode}
            onSetVisionModel={props.onSetVisionModel}
          />

          <form
            className="flex shrink-0 gap-3 border-t border-ink/15 bg-[#eeeee6] px-5 py-4"
            onSubmit={props.onSend}
          >
            <textarea
              className="max-h-32 min-h-12 flex-1 resize-none rounded-md border border-ink/20 bg-white px-3 py-3 text-sm outline-none focus:border-tide"
              value={props.draft}
              onChange={(event) => props.onSetDraft(event.target.value)}
              placeholder={
                props.pendingSources.length > 0
                  ? "Ask about the attachments"
                  : props.conversationContext.length > 0
                    ? "Ask about what's in conversation"
                    : props.useRag
                      ? "Ask with Sources"
                      : "Message"
              }
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
            />
            <button
              className="flex h-12 w-12 shrink-0 items-center justify-center rounded-md bg-moss text-white hover:bg-[#35543d]"
              disabled={props.busy || (!props.draft.trim() && props.pendingSources.length === 0 && props.conversationContext.length === 0)}
              title="Send"
              type="submit"
            >
              <Send size={18} aria-hidden="true" />
            </button>
          </form>
        </>
      )}
    </>
  );
}

function DocumentsWorkspace(props: {
  busy: boolean;
  documents: DocumentRecord[];
  error: string | null;
  importPath: string;
  legacyPath: string;
  legacyReport: LegacyImportReport | null;
  lastIndexResult: RAGIndexResult | null;
  lastOcrResult: OCRResult | null;
  ocrStatus: OCRStatus | null;
  ragHealth: RAGHealth | null;
  searchQuery: string;
  searchResults: RAGSearchResult[];
  onDelete: (documentId: string) => void;
  onChooseDocument: () => void;
  onChooseLegacyFolder: () => void;
  onImport: (event: FormEvent) => void;
  onImportLegacy: (event: FormEvent) => void;
  onReindex: (documentId: string) => void;
  onRefreshOcrStatus: () => void;
  onRunOcr: (documentId: string) => void;
  onSearch: (event?: FormEvent) => void;
  onSetImportPath: (value: string) => void;
  onSetLegacyPath: (value: string) => void;
  onSetSearchQuery: (value: string) => void;
}) {
  return (
    <>
      <header className="flex h-16 shrink-0 items-center justify-between border-b border-ink/15 px-5">
        <div>
          <h2 className="text-lg font-semibold">Documents</h2>
          <p className="text-xs text-ink/55">
            {props.ragHealth
              ? `${props.ragHealth.documents} document(s), ${props.ragHealth.chunks} chunk(s), ${props.ragHealth.cached_embeddings} cached embedding(s)`
              : "Index status unavailable"}
          </p>
        </div>
      </header>

      <ErrorBanner error={props.error} />

      <div className="scrollbar-thin min-h-0 flex-1 overflow-auto px-5 py-5">
        <div className="grid grid-cols-[minmax(280px,380px)_minmax(0,1fr)] gap-5">
          <section className="space-y-4">
            <form className="rounded-md border border-ink/15 bg-white p-4" onSubmit={props.onImport}>
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
                <Upload size={17} aria-hidden="true" />
                Import
              </div>
              <div className="flex gap-2">
                <input
                  className="min-w-0 flex-1 rounded-md border border-ink/20 px-3 py-2 text-sm outline-none focus:border-tide"
                  onChange={(event) => props.onSetImportPath(event.target.value)}
                  placeholder="C:\\path\\to\\document.txt"
                  value={props.importPath}
                />
                <button
                  className="flex h-10 shrink-0 items-center justify-center gap-2 rounded-md border border-ink/15 bg-white px-3 text-sm font-medium hover:bg-[#faf9f3]"
                  disabled={props.busy}
                  onClick={props.onChooseDocument}
                  type="button"
                >
                  <FileText size={16} aria-hidden="true" />
                  Browse
                </button>
              </div>
              <p className="mt-2 text-xs text-ink/55">Supported now: .txt, .md, extractable .pdf</p>
              <div className="mt-3 rounded-md border border-dashed border-ink/20 bg-[#faf9f3] px-3 py-4 text-center text-xs text-ink/55">
                Drop a supported document here to import it.
              </div>
              <button
                className="mt-3 flex h-10 w-full items-center justify-center gap-2 rounded-md bg-moss px-3 text-sm font-medium text-white hover:bg-[#35543d]"
                disabled={props.busy || !props.importPath.trim()}
                type="submit"
              >
                <Upload size={16} aria-hidden="true" />
                Import and index
              </button>
            </form>

            <OCRDebugStatus
              busy={props.busy}
              onRefresh={props.onRefreshOcrStatus}
              status={props.ocrStatus}
            />

            <form className="rounded-md border border-ink/15 bg-white p-4" onSubmit={props.onSearch}>
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
                <Search size={17} aria-hidden="true" />
                Test Retrieval
              </div>
              <textarea
                className="min-h-20 w-full resize-none rounded-md border border-ink/20 px-3 py-2 text-sm outline-none focus:border-tide"
                onChange={(event) => props.onSetSearchQuery(event.target.value)}
                placeholder="Search the local document index"
                value={props.searchQuery}
              />
              <button
                className="mt-3 flex h-10 w-full items-center justify-center gap-2 rounded-md bg-tide px-3 text-sm font-medium text-white hover:bg-[#2e5c66]"
                disabled={props.busy || !props.searchQuery.trim()}
                type="submit"
              >
                <Search size={16} aria-hidden="true" />
                Search chunks
              </button>
            </form>

            {props.lastIndexResult && (
              <div className="rounded-md border border-tide/20 bg-[#eef8f8] p-4 text-sm">
                <p className="font-medium">{props.lastIndexResult.document.title}</p>
                <p className="mt-1 text-xs text-tide">
                  {props.lastIndexResult.chunks.length} chunk(s), {props.lastIndexResult.embedded} embedded,{" "}
                  {props.lastIndexResult.cached} from cache
                </p>
              </div>
            )}

            {props.lastOcrResult && (
              <OCRResultSummary result={props.lastOcrResult} />
            )}

            <form className="rounded-md border border-ink/15 bg-white p-4" onSubmit={props.onImportLegacy}>
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
                <Database size={17} aria-hidden="true" />
                Legacy Import
              </div>
              <div className="flex gap-2">
                <input
                  className="min-w-0 flex-1 rounded-md border border-ink/20 px-3 py-2 text-sm outline-none focus:border-tide"
                  onChange={(event) => props.onSetLegacyPath(event.target.value)}
                  placeholder="C:\\path\\to\\old\\odysseus"
                  value={props.legacyPath}
                />
                <button
                  className="flex h-10 shrink-0 items-center justify-center gap-2 rounded-md border border-ink/15 bg-white px-3 text-sm font-medium hover:bg-[#faf9f3]"
                  disabled={props.busy}
                  onClick={props.onChooseLegacyFolder}
                  type="button"
                >
                  <FileText size={16} aria-hidden="true" />
                  Folder
                </button>
              </div>
              <p className="mt-2 text-xs text-ink/55">
                Reads old data and copies compatible items into this profile.
              </p>
              <button
                className="mt-3 flex h-10 w-full items-center justify-center gap-2 rounded-md bg-gold px-3 text-sm font-medium text-white hover:bg-[#a8772b]"
                disabled={props.busy || !props.legacyPath.trim()}
                type="submit"
              >
                <Upload size={16} aria-hidden="true" />
                Import legacy data
              </button>
            </form>

            {props.legacyReport && <LegacyReportView report={props.legacyReport} />}
          </section>

          <section className="min-w-0 space-y-4">
            <div className="rounded-md border border-ink/15 bg-white">
              <div className="flex items-center gap-2 border-b border-ink/10 px-4 py-3 text-sm font-semibold">
                <FileText size={17} aria-hidden="true" />
                Document List
              </div>
              {props.documents.length === 0 ? (
                <p className="px-4 py-6 text-sm text-ink/55">No documents imported yet.</p>
              ) : (
                <div className="divide-y divide-ink/10">
                  {props.documents.map((document) => (
                    <DocumentRow
                      busy={props.busy}
                      document={document}
                      key={document.id}
                      ocrStatus={props.ocrStatus}
                      onDelete={props.onDelete}
                      onReindex={props.onReindex}
                      onRunOcr={props.onRunOcr}
                    />
                  ))}
                </div>
              )}
            </div>

            <div className="rounded-md border border-ink/15 bg-white">
              <div className="flex items-center gap-2 border-b border-ink/10 px-4 py-3 text-sm font-semibold">
                <Search size={17} aria-hidden="true" />
                Retrieval Results
              </div>
              {props.searchResults.length === 0 ? (
                <p className="px-4 py-6 text-sm text-ink/55">Run a search to inspect retrieved chunks.</p>
              ) : (
                <div className="divide-y divide-ink/10">
                  {props.searchResults.map((result) => (
                    <div className="px-4 py-3" key={result.chunk_id}>
                      <div className="flex items-center justify-between gap-3 text-xs text-ink/55">
                        <span className="truncate">
                          {String(result.metadata.title ?? result.metadata.file_name ?? "Document")}
                          {result.page_start ? `, page ${result.page_start}` : ""}
                        </span>
                        <span>{result.score.toFixed(3)}</span>
                      </div>
                      <p className="mt-2 line-clamp-4 text-sm leading-6">{result.content}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>
        </div>
      </div>
    </>
  );
}

function DiagnosticsWorkspace(props: {
  activeCampaign: BenchmarkCampaign | null;
  benchmarkModel: string;
  benchmarkMode: BenchmarkMode;
  benchmarkResult: EvalRun | null;
  benchmarkRepeats: 1 | 3;
  benchmarkThinking: Exclude<ThinkingMode, "legacy/unrecorded">;
  benchmarkVerify: boolean;
  busy: boolean;
  campaignAutoReport: boolean;
  campaignBusy: boolean;
  campaignModels: CampaignModelInfo[];
  campaignModes: BenchmarkMode[];
  campaignOutputFolder: string;
  campaignPlan: CampaignPlan | null;
  campaignPreset: CampaignPreset;
  campaignRepeats: 1 | 3;
  campaignSelectedModels: string[];
  campaignStatusMessage: string;
  campaignThinkingModes: Array<Exclude<ThinkingMode, "legacy/unrecorded">>;
  campaignTitle: string;
  campaignVerifierSettings: boolean[];
  campaigns: BenchmarkCampaign[];
  comparison: BenchmarkComparison | null;
  copyStatus: string;
  diagnostics: DiagnosticsStatus | null;
  progressEvents: OperationProgressEvent[];
  error: string | null;
  evalHistory: EvalRun[];
  evalSuite: EvalSuite | null;
  imageCopyStatus: string;
  imageEvalHistory: ImageEvalRun[];
  imageEvalMode: MultimodalMode;
  imageEvalModel: string;
  imageEvalResult: ImageEvalRun | null;
  imageEvalSuite: ImageEvalSuite | null;
  selectedVisionModel: string;
  visionModels: ModelCapability[];
  onCancelCampaign: (campaign: BenchmarkCampaign) => void;
  onClearHistory: () => void;
  onClearCampaignModels: () => void;
  onCopySummary: () => void;
  onChooseCampaignOutputFolder: () => void;
  onDeleteCampaign: (campaign: BenchmarkCampaign) => void;
  onGenerateCampaignReport: (campaign: BenchmarkCampaign) => void;
  onOpenCampaignReport: (campaign: BenchmarkCampaign, kind: "pdf" | "html" | "folder") => void;
  onPauseCampaign: (campaign: BenchmarkCampaign) => void;
  onRefresh: () => void;
  onRefreshCampaign: (campaignId: string) => void;
  onRemovePlannedCampaignJob: (job: CampaignJob) => void;
  onResumeCampaign: (campaign: BenchmarkCampaign) => void;
  onRetryCampaignJob: (job: CampaignJob) => void;
  onReviewCampaignPlan: () => void;
  onRunBenchmark: () => void;
  onSelectAllChatCampaignModels: () => void;
  onSetBenchmarkMode: (value: BenchmarkMode) => void;
  onSetBenchmarkModel: (value: string) => void;
  onSetBenchmarkRepeats: (value: 1 | 3) => void;
  onSetBenchmarkThinking: (value: Exclude<ThinkingMode, "legacy/unrecorded">) => void;
  onSetBenchmarkVerify: (value: boolean) => void;
  onCopyImageSummary: () => void;
  onRefreshModelCapabilities: () => void;
  onRunImageBenchmark: () => void;
  onSetImageEvalMode: (value: MultimodalMode) => void;
  onSetImageEvalModel: (value: string) => void;
  onSetCampaignAutoReport: (value: boolean) => void;
  onSetCampaignModes: (value: BenchmarkMode[]) => void;
  onSetCampaignOutputFolder: (value: string) => void;
  onSetCampaignPreset: (value: CampaignPreset) => void;
  onSetCampaignRepeats: (value: 1 | 3) => void;
  onSetCampaignSelectedModels: (value: string[]) => void;
  onSetCampaignThinkingModes: (value: Array<Exclude<ThinkingMode, "legacy/unrecorded">>) => void;
  onSetCampaignTitle: (value: string) => void;
  onSetCampaignVerifierSettings: (value: boolean[]) => void;
  onStartCampaign: () => void;
  onToggleCampaignMode: (mode: BenchmarkMode, selected: boolean) => void;
  onToggleCampaignModel: (model: string, selected: boolean) => void;
  onToggleCampaignThinking: (mode: Exclude<ThinkingMode, "legacy/unrecorded">, selected: boolean) => void;
  onToggleCampaignVerifier: (value: boolean, selected: boolean) => void;
  onOpenReadiness: () => void;
}) {
  const models = props.diagnostics?.ollama.models ?? [];
  const modelDetails = props.diagnostics?.ollama.model_details ?? [];
  const canRun = Boolean(props.diagnostics?.ollama.reachable && props.benchmarkModel.trim());
  const verifierEnabled = props.benchmarkMode === "end_to_end";
  const latestBenchmarkRun = props.benchmarkResult ?? props.evalHistory[0] ?? null;
  const retrievalMismatch = benchmarkRetrievalMismatch(props.diagnostics?.rag ?? null, latestBenchmarkRun);
  return (
    <>
      <header className="flex h-16 shrink-0 items-center justify-between border-b border-ink/15 px-5">
        <div>
          <h2 className="text-lg font-semibold">Diagnostics</h2>
          <p className="text-xs text-ink/55">
            {props.evalSuite
              ? `${props.evalSuite.case_count} eval case(s), ${props.evalHistory.length} saved run(s)`
              : "Benchmark status unavailable"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={props.onOpenReadiness}
            className="rounded border border-ink/20 px-3 py-1 text-xs font-medium text-ink/70 hover:bg-ink/5"
          >
            Check setup
          </button>
          <IconButton disabled={props.busy} onClick={props.onRefresh} title="Refresh diagnostics">
            <RefreshCw size={15} aria-hidden="true" />
          </IconButton>
        </div>
      </header>

      <ErrorBanner error={props.error} />

      <div className="scrollbar-thin min-h-0 flex-1 overflow-auto px-5 py-5">
        <div className="grid grid-cols-[minmax(280px,380px)_minmax(0,1fr)] gap-5">
          <section className="space-y-4">
            <div className="rounded-md border border-ink/15 bg-white p-4">
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
                <Cpu size={17} aria-hidden="true" />
                Runtime
              </div>
              <div className="space-y-3 text-sm">
                <DiagnosticRow label="App version" value={props.diagnostics?.app_version ?? "checking"} />
                <DiagnosticRow
                  label="Backend"
                  value={props.diagnostics?.backend_ready ? "ready" : "starting"}
                />
                <DiagnosticRow label="Current model" value={props.diagnostics?.current_model ?? ""} />
                <PathRow label="Profile" value={props.diagnostics?.profile_dir ?? ""} />
                <PathRow label="Database" value={props.diagnostics?.db_path ?? ""} />
                <PathRow label="Backend log" value={props.diagnostics?.backend_log_path ?? ""} />
              </div>
            </div>

            <OperationProgressDiagnostics events={props.progressEvents} />

            <div className="rounded-md border border-ink/15 bg-white p-4">
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
                <Search size={17} aria-hidden="true" />
                App Document Retrieval
              </div>
              <div className="space-y-3 text-sm">
                <DiagnosticRow label="Backend" value={props.diagnostics?.rag.embedding.backend ?? ""} />
                <DiagnosticRow label="Model" value={props.diagnostics?.rag.embedding.model ?? ""} />
                <DiagnosticRow
                  label="Semantic active"
                  value={props.diagnostics?.rag.embedding.semantic ? "yes" : "no"}
                />
                <DiagnosticRow
                  label="Indexed with active backend"
                  value={formatIndexedWithActiveBackend(props.diagnostics?.rag ?? null)}
                />
                <DiagnosticRow
                  label="Needs reindex"
                  value={props.diagnostics?.rag.documents_needing_reindex ?? 0}
                />
              </div>
              {props.diagnostics?.rag.embedding.message && (
                <p className="mt-3 text-xs text-ink/55">{props.diagnostics.rag.embedding.message}</p>
              )}
            </div>

            <div className="rounded-md border border-ink/15 bg-white p-4">
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
                <BarChart3 size={17} aria-hidden="true" />
                Benchmark Retrieval
              </div>
              <div className="space-y-3 text-sm">
                <DiagnosticRow label="Backend used" value={latestBenchmarkRun?.embedding_backend ?? "no runs"} />
                <DiagnosticRow label="Embedding model" value={latestBenchmarkRun?.embedding_model ?? "no runs"} />
                <DiagnosticRow
                  label="Semantic used"
                  value={latestBenchmarkRun ? (latestBenchmarkRun.embedding_backend === "semantic" ? "yes" : "no") : "no runs"}
                />
                <DiagnosticRow
                  label="Eval suite"
                  value={latestBenchmarkRun?.suite_version ?? props.evalSuite?.suite_version ?? "checking"}
                />
              </div>
              {retrievalMismatch && (
                <p className="mt-3 rounded-md border border-gold/30 bg-[#fff8e8] px-3 py-2 text-xs text-[#7a561d]">
                  Benchmark used semantic retrieval, but your document library is currently lexical/not reindexed.
                </p>
              )}
            </div>

            <div className="rounded-md border border-ink/15 bg-white p-4">
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
                <Power size={17} aria-hidden="true" />
                Ollama Models
              </div>
              <RuntimeStatus status={props.diagnostics?.ollama ?? null} />
              {models.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-2">
                  {models.map((model) => (
                    <span className="rounded-md border border-tide/20 bg-[#eef8f8] px-2 py-1 text-xs text-tide" key={model}>
                      {model}
                    </span>
                  ))}
                </div>
              )}
              {modelDetails.length > 0 && <ModelStatsTable details={modelDetails} />}
            </div>

            <OCRDebugStatus
              busy={props.busy}
              onRefresh={props.onRefresh}
              status={props.diagnostics?.ocr ?? null}
            />

            <ImageVisionDiagnostics
              busy={props.busy}
              data={props.diagnostics?.image_vision}
              selectedVisionModel={props.selectedVisionModel}
              onRefreshModels={props.onRefreshModelCapabilities}
            />
          </section>

          <section className="min-w-0 space-y-4">
            <BenchmarkCampaignPanel
              activeCampaign={props.activeCampaign}
              autoReport={props.campaignAutoReport}
              busy={props.campaignBusy}
              campaigns={props.campaigns}
              modes={props.campaignModes}
              models={props.campaignModels}
              outputFolder={props.campaignOutputFolder}
              plan={props.campaignPlan}
              preset={props.campaignPreset}
              repeats={props.campaignRepeats}
              selectedModels={props.campaignSelectedModels}
              statusMessage={props.campaignStatusMessage}
              thinkingModes={props.campaignThinkingModes}
              title={props.campaignTitle}
              verifierSettings={props.campaignVerifierSettings}
              onCancel={props.onCancelCampaign}
              onChooseOutputFolder={props.onChooseCampaignOutputFolder}
              onClearModels={props.onClearCampaignModels}
              onDeleteCampaign={props.onDeleteCampaign}
              onGenerateReport={props.onGenerateCampaignReport}
              onOpenReport={props.onOpenCampaignReport}
              onPause={props.onPauseCampaign}
              onRefreshCampaign={props.onRefreshCampaign}
              onRemoveJob={props.onRemovePlannedCampaignJob}
              onResume={props.onResumeCampaign}
              onRetryJob={props.onRetryCampaignJob}
              onReviewPlan={props.onReviewCampaignPlan}
              onSelectAllChatModels={props.onSelectAllChatCampaignModels}
              onSetAutoReport={props.onSetCampaignAutoReport}
              onSetOutputFolder={props.onSetCampaignOutputFolder}
              onSetPreset={props.onSetCampaignPreset}
              onSetRepeats={props.onSetCampaignRepeats}
              onSetTitle={props.onSetCampaignTitle}
              onStart={props.onStartCampaign}
              onToggleMode={props.onToggleCampaignMode}
              onToggleModel={props.onToggleCampaignModel}
              onToggleThinking={props.onToggleCampaignThinking}
              onToggleVerifier={props.onToggleCampaignVerifier}
            />

            <ImageBenchmarkPanel
              busy={props.busy}
              copyStatus={props.imageCopyStatus}
              history={props.imageEvalHistory}
              mode={props.imageEvalMode}
              model={props.imageEvalModel}
              result={props.imageEvalResult}
              suite={props.imageEvalSuite}
              visionModels={props.visionModels}
              onCopySummary={props.onCopyImageSummary}
              onRun={props.onRunImageBenchmark}
              onSetMode={props.onSetImageEvalMode}
              onSetModel={props.onSetImageEvalModel}
            />

            <div className="rounded-md border border-ink/15 bg-white p-4">
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
                <BarChart3 size={17} aria-hidden="true" />
                Model Benchmark
              </div>
              <p className="mb-2 text-xs text-ink/55">
                Benchmarks use bundled local eval fixtures, not your imported Sources library.
              </p>
              <div className="grid grid-cols-[minmax(160px,1fr)_150px_130px_90px] items-center gap-3">
                <select
                  className="h-10 min-w-0 rounded-md border border-ink/20 bg-white px-3 text-sm outline-none focus:border-tide"
                  disabled={props.busy || models.length === 0}
                  onChange={(event) => props.onSetBenchmarkModel(event.target.value)}
                  value={props.benchmarkModel}
                >
                  {models.length === 0 ? (
                    <option value="">No installed Ollama models</option>
                  ) : (
                    models.map((model) => (
                      <option key={model} value={model}>
                        {model}
                      </option>
                    ))
                  )}
                </select>
                <select
                  className="h-10 rounded-md border border-ink/20 bg-white px-3 text-sm outline-none focus:border-tide"
                  disabled={props.busy}
                  onChange={(event) => props.onSetBenchmarkMode(event.target.value as BenchmarkMode)}
                  value={props.benchmarkMode}
                >
                  {BENCHMARK_MODE_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
                <select
                  className="h-10 rounded-md border border-ink/20 bg-white px-3 text-sm outline-none focus:border-tide"
                  disabled={props.busy || props.benchmarkMode === "retrieval_only"}
                  onChange={(event) => props.onSetBenchmarkThinking(event.target.value as Exclude<ThinkingMode, "legacy/unrecorded">)}
                  value={props.benchmarkThinking}
                >
                  {THINKING_MODE_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      Thinking {option.label}
                    </option>
                  ))}
                </select>
                <select
                  className="h-10 rounded-md border border-ink/20 bg-white px-3 text-sm outline-none focus:border-tide"
                  disabled={props.busy}
                  onChange={(event) => props.onSetBenchmarkRepeats(Number(event.target.value) === 3 ? 3 : 1)}
                  value={props.benchmarkRepeats}
                >
                  <option value={1}>1 run</option>
                  <option value={3}>3 runs</option>
                </select>
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-3">
                <label className="flex h-10 items-center gap-2 rounded-md border border-ink/15 bg-white px-3 text-sm">
                  <input
                    checked={verifierEnabled && props.benchmarkVerify}
                    className="h-4 w-4 accent-tide"
                    disabled={props.busy || !verifierEnabled}
                    onChange={(event) => props.onSetBenchmarkVerify(event.target.checked)}
                    type="checkbox"
                  />
                  Verify
                </label>
                <button
                  className="flex h-10 items-center justify-center gap-2 rounded-md bg-moss px-3 text-sm font-medium text-white hover:bg-[#35543d]"
                  disabled={props.busy || !canRun}
                  onClick={props.onRunBenchmark}
                  type="button"
                >
                  <BarChart3 size={16} aria-hidden="true" />
                  Run
                </button>
                <button
                  className="flex h-10 items-center justify-center gap-2 rounded-md border border-ink/15 bg-white px-3 text-sm font-medium hover:bg-[#faf9f3]"
                  disabled={props.busy || (!props.benchmarkResult && props.evalHistory.length === 0)}
                  onClick={props.onCopySummary}
                  type="button"
                >
                  <Clipboard size={16} aria-hidden="true" />
                  Copy
                </button>
              </div>
              <div className="mt-3 grid grid-cols-5 gap-2 text-xs text-ink/65">
                <Metric label="Suite" value={props.evalSuite?.suite_name ?? "local-rag"} />
                <Metric label="Version" value={props.evalSuite?.suite_version ?? "checking"} />
                <Metric label="Cases" value={props.evalSuite?.case_count ?? 0} />
                <Metric label="History" value={props.evalHistory.length} />
                <Metric label="Latest run" value={latestBenchmarkRun ? formatBenchmarkRunLabel(latestBenchmarkRun) : "none"} />
              </div>
              <p className="mt-2 text-xs text-ink/55">
                Eval documents are temporary/internal to the benchmark and will not appear in Sources.
              </p>
              <p className="mt-1 text-xs text-ink/55">
                Retrieval-only measures ranking without a chat model. Oracle generation bypasses retrieval. Verifier applies only to end-to-end runs.
              </p>
              {!props.diagnostics?.ollama.reachable && (
                <p className="mt-3 text-xs text-clay">Ollama is not reachable at 127.0.0.1:11434.</p>
              )}
              {props.copyStatus && <p className="mt-3 text-xs text-moss">{props.copyStatus}</p>}
            </div>

            {props.benchmarkResult && <BenchmarkRunCard run={props.benchmarkResult} title="Latest Result" />}

            {props.comparison && <BenchmarkComparisonCard comparison={props.comparison} />}

            <div className="rounded-md border border-ink/15 bg-white">
              <div className="flex items-center justify-between border-b border-ink/10 px-4 py-3">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <History size={17} aria-hidden="true" />
                  Benchmark History
                </div>
                <button
                  className="rounded-md border border-ink/15 bg-white px-3 py-1.5 text-xs font-medium hover:bg-[#faf9f3]"
                  disabled={props.busy || props.evalHistory.length === 0}
                  onClick={props.onClearHistory}
                  type="button"
                >
                  Clear
                </button>
              </div>
              {props.evalHistory.length === 0 ? (
                <p className="px-4 py-6 text-sm text-ink/55">No benchmark runs yet.</p>
              ) : (
                <div className="divide-y divide-ink/10">
                  {props.evalHistory.map((run) => (
                    <div className="px-4 py-3" key={run.id}>
                      <div className="flex items-center justify-between gap-3">
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium">{run.model}</p>
                          <p className="text-xs text-ink/55">
                            {formatTimestamp(run.created_at)} - {formatBenchmarkRunLabel(run)}
                          </p>
                        </div>
                        <div className="text-right text-xs">
                          <p className={run.total_failed === 0 ? "text-moss" : "text-clay"}>
                            {run.total_passed} passed, {run.total_failed} failed
                          </p>
                          <p className="text-ink/55">{run.average_latency_ms} ms avg</p>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>
        </div>
      </div>
    </>
  );
}

function BenchmarkCampaignPanel(props: {
  activeCampaign: BenchmarkCampaign | null;
  autoReport: boolean;
  busy: boolean;
  campaigns: BenchmarkCampaign[];
  modes: BenchmarkMode[];
  models: CampaignModelInfo[];
  outputFolder: string;
  plan: CampaignPlan | null;
  preset: CampaignPreset;
  repeats: 1 | 3;
  selectedModels: string[];
  statusMessage: string;
  thinkingModes: Array<Exclude<ThinkingMode, "legacy/unrecorded">>;
  title: string;
  verifierSettings: boolean[];
  onCancel: (campaign: BenchmarkCampaign) => void;
  onChooseOutputFolder: () => void;
  onClearModels: () => void;
  onDeleteCampaign: (campaign: BenchmarkCampaign) => void;
  onGenerateReport: (campaign: BenchmarkCampaign) => void;
  onOpenReport: (campaign: BenchmarkCampaign, kind: "pdf" | "html" | "folder") => void;
  onPause: (campaign: BenchmarkCampaign) => void;
  onRefreshCampaign: (campaignId: string) => void;
  onRemoveJob: (job: CampaignJob) => void;
  onResume: (campaign: BenchmarkCampaign) => void;
  onRetryJob: (job: CampaignJob) => void;
  onReviewPlan: () => void;
  onSelectAllChatModels: () => void;
  onSetAutoReport: (value: boolean) => void;
  onSetOutputFolder: (value: string) => void;
  onSetPreset: (value: CampaignPreset) => void;
  onSetRepeats: (value: 1 | 3) => void;
  onSetTitle: (value: string) => void;
  onStart: () => void;
  onToggleMode: (mode: BenchmarkMode, selected: boolean) => void;
  onToggleModel: (model: string, selected: boolean) => void;
  onToggleThinking: (mode: Exclude<ThinkingMode, "legacy/unrecorded">, selected: boolean) => void;
  onToggleVerifier: (value: boolean, selected: boolean) => void;
}) {
  const thorough = props.preset === "thorough";
  const canStart = Boolean(props.plan && props.plan.planned_jobs.length > 0 && props.selectedModels.length > 0);
  return (
    <div className="rounded-md border border-ink/15 bg-white p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold">
            <BarChart3 size={17} aria-hidden="true" />
            Benchmark Campaigns
          </div>
          <p className="mt-1 text-xs text-ink/55">
            Plan queued local benchmarks, run one job at a time, and export PDF/HTML/JSON reports.
          </p>
        </div>
        {props.activeCampaign && (
          <button
            className="rounded-md border border-ink/15 bg-white px-3 py-1.5 text-xs font-medium hover:bg-[#faf9f3]"
            disabled={props.busy}
            onClick={() => props.onRefreshCampaign(props.activeCampaign!.id)}
            type="button"
          >
            Refresh
          </button>
        )}
      </div>

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1.1fr)_minmax(320px,0.9fr)]">
        <div className="space-y-3">
          <div className="grid gap-3 md:grid-cols-[minmax(180px,1fr)_180px]">
            <label className="block text-xs font-medium text-ink/65">
              Campaign title
              <input
                className="mt-1 h-10 w-full rounded-md border border-ink/20 px-3 text-sm outline-none focus:border-tide"
                onChange={(event) => props.onSetTitle(event.target.value)}
                value={props.title}
              />
            </label>
            <label className="block text-xs font-medium text-ink/65">
              Preset
              <select
                className="mt-1 h-10 w-full rounded-md border border-ink/20 bg-white px-3 text-sm outline-none focus:border-tide"
                onChange={(event) => props.onSetPreset(event.target.value as CampaignPreset)}
                value={props.preset}
              >
                {CAMPAIGN_PRESET_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="rounded-md border border-ink/10 p-3">
            <div className="mb-2 flex items-center justify-between gap-3">
              <p className="text-xs font-semibold uppercase text-ink/50">Installed chat model selection</p>
              <div className="flex gap-2">
                <button className="rounded-md border border-ink/15 px-2 py-1 text-xs hover:bg-[#faf9f3]" onClick={props.onSelectAllChatModels} type="button">
                  Select chat
                </button>
                <button className="rounded-md border border-ink/15 px-2 py-1 text-xs hover:bg-[#faf9f3]" onClick={props.onClearModels} type="button">
                  Clear
                </button>
              </div>
            </div>
            <div className="grid gap-2 md:grid-cols-2">
              {props.models.length === 0 ? (
                <p className="text-sm text-ink/55">No Ollama models reported yet.</p>
              ) : (
                props.models.map((model) => {
                  const selected = props.selectedModels.includes(model.name);
                  const embeddingOnly = model.capability === "embedding";
                  return (
                    <label className="flex min-h-[78px] gap-2 rounded-md border border-ink/10 p-2 text-sm" key={model.name}>
                      <input
                        checked={selected}
                        className="mt-1 h-4 w-4 accent-tide"
                        disabled={embeddingOnly}
                        onChange={(event) => props.onToggleModel(model.name, event.target.checked)}
                        type="checkbox"
                      />
                      <span className="min-w-0">
                        <span className="block truncate font-medium" title={model.name}>
                          {model.name}
                        </span>
                        <span className="mt-1 block text-xs text-ink/55">
                          {model.capability} - {model.parameter_size || "unknown params"} - {model.quantization_level || "unknown quant"} - {formatBytes(model.size)}
                        </span>
                        <span className="mt-1 block text-xs text-ink/55">
                          {formatOffload(model)}
                          {model.historical_average_latency_ms ? ` - history ${formatMs(model.historical_average_latency_ms)}` : ""}
                        </span>
                        {model.warning && <span className="mt-1 block text-xs text-clay">{model.warning}</span>}
                      </span>
                    </label>
                  );
                })
              )}
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <CheckboxGroup
              disabled={!thorough}
              label="Modes"
              options={BENCHMARK_MODE_OPTIONS.map((option) => ({ value: option.value, label: option.label }))}
              selected={props.modes}
              onToggle={(value, selected) => props.onToggleMode(value as BenchmarkMode, selected)}
            />
            <CheckboxGroup
              disabled={!thorough}
              label="Thinking"
              options={THINKING_MODE_OPTIONS.map((option) => ({ value: option.value, label: option.label }))}
              selected={props.thinkingModes}
              onToggle={(value, selected) => props.onToggleThinking(value as Exclude<ThinkingMode, "legacy/unrecorded">, selected)}
            />
            <CheckboxGroup
              disabled={!thorough}
              label="Verifier"
              options={[
                { value: "false", label: "Off" },
                { value: "true", label: "On" }
              ]}
              selected={props.verifierSettings.map(String)}
              onToggle={(value, selected) => props.onToggleVerifier(value === "true", selected)}
            />
            <label className="block rounded-md border border-ink/10 p-3 text-xs font-medium text-ink/65">
              Repeats
              <select
                className="mt-2 h-9 w-full rounded-md border border-ink/20 bg-white px-2 text-sm outline-none focus:border-tide"
                disabled={!thorough}
                onChange={(event) => props.onSetRepeats(Number(event.target.value) === 3 ? 3 : 1)}
                value={props.repeats}
              >
                <option value={1}>1 run</option>
                <option value={3}>3 runs</option>
              </select>
            </label>
          </div>

          <div className="grid gap-3 md:grid-cols-[1fr_auto]">
            <label className="block text-xs font-medium text-ink/65">
              Report output folder
              <input
                className="mt-1 h-10 w-full rounded-md border border-ink/20 px-3 text-sm outline-none focus:border-tide"
                onChange={(event) => props.onSetOutputFolder(event.target.value)}
                placeholder="Downloads\\Odysseus Reports"
                value={props.outputFolder}
              />
            </label>
            <button
              className="mt-5 flex h-10 items-center justify-center gap-2 rounded-md border border-ink/15 bg-white px-3 text-sm font-medium hover:bg-[#faf9f3]"
              onClick={props.onChooseOutputFolder}
              type="button"
            >
              <FolderOpen size={15} aria-hidden="true" />
              Choose
            </button>
          </div>

          <label className="flex items-center gap-2 text-sm">
            <input
              checked={props.autoReport}
              className="h-4 w-4 accent-tide"
              onChange={(event) => props.onSetAutoReport(event.target.checked)}
              type="checkbox"
            />
            Generate report automatically when finished
          </label>

          <div className="flex flex-wrap gap-2">
            <button
              className="flex h-10 items-center gap-2 rounded-md border border-ink/15 bg-white px-3 text-sm font-medium hover:bg-[#faf9f3]"
              disabled={props.busy || props.selectedModels.length === 0}
              onClick={props.onReviewPlan}
              type="button"
            >
              <Clipboard size={15} aria-hidden="true" />
              Review plan
            </button>
            <button
              className="flex h-10 items-center gap-2 rounded-md bg-moss px-3 text-sm font-medium text-white hover:bg-[#35543d]"
              disabled={props.busy || !canStart}
              onClick={props.onStart}
              type="button"
            >
              <Play size={15} aria-hidden="true" />
              Start
            </button>
          </div>
          {props.statusMessage && <p className="text-xs text-moss">{props.statusMessage}</p>}
        </div>

        <div className="space-y-3">
          <CampaignPlanSummary plan={props.plan} onRemoveJob={props.onRemoveJob} />
          <ActiveCampaignCard
            campaign={props.activeCampaign}
            busy={props.busy}
            onCancel={props.onCancel}
            onGenerateReport={props.onGenerateReport}
            onOpenReport={props.onOpenReport}
            onPause={props.onPause}
            onResume={props.onResume}
            onRetryJob={props.onRetryJob}
          />
        </div>
      </div>

      <CampaignHistory
        campaigns={props.campaigns}
        onDeleteCampaign={props.onDeleteCampaign}
        onGenerateReport={props.onGenerateReport}
        onOpenReport={props.onOpenReport}
        onResume={props.onResume}
      />
    </div>
  );
}

function CheckboxGroup(props: {
  disabled?: boolean;
  label: string;
  options: Array<{ value: string; label: string }>;
  selected: string[];
  onToggle: (value: string, selected: boolean) => void;
}) {
  return (
    <div className="rounded-md border border-ink/10 p-3">
      <p className="text-xs font-semibold uppercase text-ink/50">{props.label}</p>
      <div className="mt-2 space-y-2">
        {props.options.map((option) => (
          <label className="flex items-center gap-2 text-sm" key={option.value}>
            <input
              checked={props.selected.includes(option.value)}
              className="h-4 w-4 accent-tide"
              disabled={props.disabled}
              onChange={(event) => props.onToggle(option.value, event.target.checked)}
              type="checkbox"
            />
            {option.label}
          </label>
        ))}
      </div>
    </div>
  );
}

function CampaignPlanSummary(props: { plan: CampaignPlan | null; onRemoveJob: (job: CampaignJob) => void }) {
  if (!props.plan) {
    return (
      <div className="rounded-md border border-ink/10 p-3">
        <p className="text-sm font-semibold">Planned jobs</p>
        <p className="mt-2 text-sm text-ink/55">Review a plan before starting a campaign.</p>
      </div>
    );
  }
  return (
    <div className="rounded-md border border-ink/10 p-3">
      <div className="grid grid-cols-3 gap-2 text-xs">
        <Metric label="Jobs" value={props.plan.planned_jobs.length} />
        <Metric label="Likely runtime" value={formatDuration(props.plan.estimate.likely_ms)} />
        <Metric label="Generations" value={props.plan.estimate.model_generation_count} />
      </div>
      <p className="mt-2 text-xs text-ink/55">
        {props.plan.estimate.source_detail ?? "No compatible history; conservative estimate"}
        {props.plan.estimate.confidence ? ` (${props.plan.estimate.confidence} confidence)` : ""}
      </p>
      {props.plan.warnings.length > 0 && (
        <div className="mt-3 space-y-1">
          {props.plan.warnings.map((warning) => (
            <p className="rounded-md border border-gold/30 bg-[#fff8e8] px-2 py-1 text-xs text-[#7a561d]" key={`${warning.level}-${warning.message}`}>
              {warning.message}
            </p>
          ))}
        </div>
      )}
      <div className="mt-3 max-h-[260px] overflow-auto rounded-md border border-ink/10">
        <table className="w-full min-w-[560px] text-left text-xs">
          <thead className="bg-[#faf9f3] text-ink/55">
            <tr>
              <th className="px-2 py-2 font-medium">#</th>
              <th className="px-2 py-2 font-medium">Model</th>
              <th className="px-2 py-2 font-medium">Mode</th>
              <th className="px-2 py-2 font-medium">Thinking</th>
              <th className="px-2 py-2 font-medium">Verify</th>
              <th className="px-2 py-2 font-medium">ETA</th>
              <th className="px-2 py-2 font-medium"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ink/10">
            {props.plan.planned_jobs.map((job, index) => (
              <tr key={job.key}>
                <td className="px-2 py-2">{index + 1}</td>
                <td className="max-w-[160px] truncate px-2 py-2" title={job.model}>{job.model}</td>
                <td className="px-2 py-2">{formatBenchmarkMode(job.benchmark_mode)}</td>
                <td className="px-2 py-2">{job.thinking_mode}</td>
                <td className="px-2 py-2">{job.verify ? "on" : "off"}</td>
                <td className="px-2 py-2" title={job.estimate_detail ?? job.estimate_source}>
                  {formatDuration(job.estimated_runtime_ms)}
                </td>
                <td className="px-2 py-2">
                  <button className="rounded-md border border-ink/15 p-1 hover:bg-[#faf9f3]" onClick={() => props.onRemoveJob(job)} title="Remove job" type="button">
                    <XCircle size={14} aria-hidden="true" />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ActiveCampaignCard(props: {
  campaign: BenchmarkCampaign | null;
  busy: boolean;
  onCancel: (campaign: BenchmarkCampaign) => void;
  onGenerateReport: (campaign: BenchmarkCampaign) => void;
  onOpenReport: (campaign: BenchmarkCampaign, kind: "pdf" | "html" | "folder") => void;
  onPause: (campaign: BenchmarkCampaign) => void;
  onResume: (campaign: BenchmarkCampaign) => void;
  onRetryJob: (job: CampaignJob) => void;
}) {
  const campaign = props.campaign;
  if (!campaign) {
    return (
      <div className="rounded-md border border-ink/10 p-3">
        <p className="text-sm font-semibold">Active campaign</p>
        <p className="mt-2 text-sm text-ink/55">No active campaign selected.</p>
      </div>
    );
  }
  const running = campaign.status === "running" || campaign.status === "queued";
  const terminal = TERMINAL_CAMPAIGN_STATUSES.has(campaign.status);
  return (
    <div className="rounded-md border border-ink/10 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold">{campaign.title}</p>
          <p className="text-xs text-ink/55">
            {campaign.status} - job {campaign.progress.job_index || 0} of {campaign.progress.job_count}
          </p>
        </div>
        <span className="rounded-md border border-ink/10 px-2 py-1 text-xs">{campaign.preset}</span>
      </div>
      <div className="mt-3 grid grid-cols-4 gap-2 text-xs">
        <Metric label="Completed" value={campaign.completed_job_count} />
        <Metric label="Failed" value={campaign.failed_job_count} />
        <Metric label="Timeouts" value={campaign.timed_out_job_count} />
        <Metric label="Elapsed" value={formatDuration(campaign.progress.elapsed_ms)} />
      </div>
      {campaign.current_job && (
        <p className="mt-3 rounded-md border border-tide/15 bg-[#eef8f8] px-2 py-1.5 text-xs text-tide">
          Current: {campaign.current_job.model} - {formatBenchmarkMode(campaign.current_job.benchmark_mode)} - thinking {campaign.current_job.thinking_mode}
        </p>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        {running && (
          <>
            <button className="flex items-center gap-1 rounded-md border border-ink/15 px-2 py-1.5 text-xs hover:bg-[#faf9f3]" disabled={props.busy} onClick={() => props.onPause(campaign)} type="button">
              <Pause size={14} aria-hidden="true" />
              Pause
            </button>
            <button className="flex items-center gap-1 rounded-md border border-clay/30 px-2 py-1.5 text-xs text-clay hover:bg-[#fff3ee]" disabled={props.busy} onClick={() => props.onCancel(campaign)} type="button">
              <XCircle size={14} aria-hidden="true" />
              Cancel
            </button>
          </>
        )}
        {(campaign.status === "paused" || campaign.status === "interrupted" || campaign.status === "draft" || campaign.status === "queued") && (
          <button className="flex items-center gap-1 rounded-md border border-ink/15 px-2 py-1.5 text-xs hover:bg-[#faf9f3]" disabled={props.busy} onClick={() => props.onResume(campaign)} type="button">
            <Play size={14} aria-hidden="true" />
            Resume
          </button>
        )}
        {terminal && (
          <>
            <button className="flex items-center gap-1 rounded-md border border-ink/15 px-2 py-1.5 text-xs hover:bg-[#faf9f3]" disabled={props.busy} onClick={() => props.onGenerateReport(campaign)} type="button">
              <FileText size={14} aria-hidden="true" />
              Regenerate report
            </button>
            <button className="rounded-md border border-ink/15 px-2 py-1.5 text-xs hover:bg-[#faf9f3]" disabled={!campaign.report_paths?.pdf} onClick={() => props.onOpenReport(campaign, "pdf")} type="button">
              Open PDF
            </button>
            <button className="rounded-md border border-ink/15 px-2 py-1.5 text-xs hover:bg-[#faf9f3]" disabled={!campaign.report_paths?.html} onClick={() => props.onOpenReport(campaign, "html")} type="button">
              Open HTML
            </button>
            <button className="rounded-md border border-ink/15 px-2 py-1.5 text-xs hover:bg-[#faf9f3]" disabled={Object.keys(campaign.report_paths || {}).length === 0} onClick={() => props.onOpenReport(campaign, "folder")} type="button">
              Open folder
            </button>
          </>
        )}
      </div>
      {campaign.jobs.some((job) => ["failed", "timed_out"].includes(job.status || "")) && (
        <div className="mt-3 space-y-1">
          {campaign.jobs
            .filter((job) => ["failed", "timed_out"].includes(job.status || ""))
            .slice(0, 3)
            .map((job) => (
              <button className="block text-left text-xs text-clay underline" key={job.id} onClick={() => props.onRetryJob(job)} type="button">
                Retry job {job.sequence}: {job.model} ({job.status})
              </button>
            ))}
        </div>
      )}
      {campaign.report_warnings.length > 0 && (
        <p className="mt-2 text-xs text-[#7a561d]">{campaign.report_warnings[0]}</p>
      )}
    </div>
  );
}

function CampaignHistory(props: {
  campaigns: BenchmarkCampaign[];
  onDeleteCampaign: (campaign: BenchmarkCampaign) => void;
  onGenerateReport: (campaign: BenchmarkCampaign) => void;
  onOpenReport: (campaign: BenchmarkCampaign, kind: "pdf" | "html" | "folder") => void;
  onResume: (campaign: BenchmarkCampaign) => void;
}) {
  return (
    <div className="mt-4 rounded-md border border-ink/10">
      <div className="border-b border-ink/10 px-3 py-2 text-sm font-semibold">Campaign history</div>
      {props.campaigns.length === 0 ? (
        <p className="px-3 py-4 text-sm text-ink/55">No campaigns yet.</p>
      ) : (
        <div className="divide-y divide-ink/10">
          {props.campaigns.slice(0, 6).map((campaign) => (
            <div className="grid gap-2 px-3 py-2 text-xs md:grid-cols-[minmax(180px,1fr)_120px_120px_auto]" key={campaign.id}>
              <div className="min-w-0">
                <p className="truncate font-medium">{campaign.title}</p>
                <p className="text-ink/55">{formatTimestamp(campaign.created_at)} - {campaign.selected_models.join(", ") || "no models"}</p>
              </div>
              <div>
                <p>{campaign.status}</p>
                <p className="text-ink/55">{campaign.completed_job_count}/{campaign.planned_job_count} jobs</p>
              </div>
              <div>
                <p>{formatDuration(campaign.actual_runtime_ms || campaign.progress.elapsed_ms)}</p>
                <p className="text-ink/55">{campaign.report_status}</p>
              </div>
              <div className="flex flex-wrap justify-end gap-1">
                {campaign.status === "interrupted" && (
                  <button className="rounded-md border border-ink/15 px-2 py-1 hover:bg-[#faf9f3]" onClick={() => props.onResume(campaign)} type="button">Resume</button>
                )}
                <button className="rounded-md border border-ink/15 px-2 py-1 hover:bg-[#faf9f3]" onClick={() => props.onGenerateReport(campaign)} type="button">Report</button>
                <button className="rounded-md border border-ink/15 px-2 py-1 hover:bg-[#faf9f3]" disabled={Object.keys(campaign.report_paths || {}).length === 0} onClick={() => props.onOpenReport(campaign, "folder")} type="button">Folder</button>
                <button className="rounded-md border border-clay/30 px-2 py-1 text-clay hover:bg-[#fff3ee]" onClick={() => props.onDeleteCampaign(campaign)} type="button">Delete</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function BenchmarkReportView({ data }: { data: CampaignReportData }) {
  const campaign = data.campaign as Record<string, unknown>;
  const view = data.view_model ?? {};
  const jobStatus = (view.job_status ?? {}) as Record<string, unknown>;
  const caseOutcomes = (view.case_outcomes ?? {}) as Record<string, unknown>;
  const comparison = data.comparison;
  const recommendations = data.recommendation ?? {};
  const balanced = recommendations.balanced as BenchmarkComparisonGroup | null | undefined;
  const groups = comparison?.groups ?? [];
  const pipelineModel = (view.pipeline_taxonomy ?? {}) as Record<string, unknown>;
  const pipeline = ((pipelineModel.counts as Record<string, number> | undefined) ?? data.pipeline_diagnoses ?? {});
  const pipelineLabels = ((pipelineModel.labels as Record<string, string> | undefined) ?? {});
  return (
    <div className="space-y-5 bg-white p-6 text-ink">
      <ReportSection name="01-executive-summary.png" label="Executive Summary">
        <p className="text-xs font-semibold uppercase text-ink/45">Odysseus Desktop Benchmark Report</p>
        <h2 className="mt-1 text-2xl font-semibold">{String(campaign.title ?? "Benchmark campaign")}</h2>
        <div className="mt-4 grid grid-cols-4 gap-3">
          <ReportMetric label="Status" value={String(campaign.status ?? "")} />
          <ReportMetric label="Jobs completed" value={`${jobStatus.completed ?? campaign.completed_job_count ?? 0}/${jobStatus.planned ?? campaign.planned_job_count ?? 0}`} />
          <ReportMetric label="Job execution failures" value={String(jobStatus.execution_failures ?? campaign.failed_job_count ?? 0)} />
          <ReportMetric label="Benchmark cases failed" value={String(caseOutcomes.failed ?? 0)} />
          <ReportMetric label="Grader review" value={String(caseOutcomes.grader_review ?? 0)} />
          <ReportMetric label="Recommended" value={balanced?.model ?? "none"} />
        </div>
        <p className="mt-4 text-sm text-ink/65">{String(recommendations.reason ?? "No eligible recommendation yet.")}</p>
      </ReportSection>

      <ReportSection name="02-model-comparison.png" label="Model Comparison">
        <h2 className="text-xl font-semibold">Model Comparison</h2>
        <table className="mt-3 w-full border-collapse text-left text-xs">
          <thead className="bg-[#edf3f0]">
            <tr>
              <th className="border border-ink/15 px-2 py-1">Model</th>
              <th className="border border-ink/15 px-2 py-1">Mode</th>
              <th className="border border-ink/15 px-2 py-1">Thinking</th>
              <th className="border border-ink/15 px-2 py-1">Latest</th>
              <th className="border border-ink/15 px-2 py-1">Mean</th>
              <th className="border border-ink/15 px-2 py-1">Latency</th>
            </tr>
          </thead>
          <tbody>
            {groups.slice(0, 8).map((group) => (
              <tr key={group.key}>
                <td className="border border-ink/15 px-2 py-1">{group.model}</td>
                <td className="border border-ink/15 px-2 py-1">{formatBenchmarkMode(group.benchmark_mode)}</td>
                <td className="border border-ink/15 px-2 py-1">{formatThinkingMode(group.thinking_mode)}</td>
                <td className="border border-ink/15 px-2 py-1">{group.latest_run_passed}/{group.latest_run_total}</td>
                <td className="border border-ink/15 px-2 py-1">{formatPercent(group.mean_pass_rate)}</td>
                <td className="border border-ink/15 px-2 py-1">{formatMs(group.median_avg_latency_ms)}</td>
              </tr>
            ))}
            {groups.length === 0 && (
              <tr>
                <td className="border border-ink/15 px-2 py-2 text-ink/55" colSpan={6}>No comparable completed runs.</td>
              </tr>
            )}
          </tbody>
        </table>
      </ReportSection>

      <ReportSection name="03-pipeline-metrics.png" label="Pipeline Metrics">
        <h2 className="text-xl font-semibold">Retrieval, Generation, End-To-End</h2>
        <div className="mt-4 grid grid-cols-4 gap-3">
          <ReportMetric label={pipelineLabels.retrieval_only ?? "retrieval-caused only"} value={String(pipeline.retrieval_only ?? 0)} />
          <ReportMetric label={pipelineLabels.generation_only ?? "generation-caused only"} value={String(pipeline.generation_only ?? 0)} />
          <ReportMetric label={pipelineLabels.both ?? "both retrieval and generation"} value={String(pipeline.both ?? 0)} />
          <ReportMetric label={pipelineLabels.grader_review ?? "grader review"} value={String(pipeline.grader_review ?? 0)} />
          <ReportMetric label="Timeouts" value={String(pipeline.timeout ?? 0)} />
          <ReportMetric label="Runtime errors" value={String(pipeline.runtime_error ?? 0)} />
          <ReportMetric label="Passed" value={String(pipeline.passed ?? 0)} />
        </div>
      </ReportSection>

      <ReportSection name="04-case-difficulty.png" label="Case Difficulty">
        <h2 className="text-xl font-semibold">Case Difficulty</h2>
        <div className="mt-3 grid grid-cols-2 gap-3">
          <ReportCaseList title="Passed in tested configurations" items={data.case_difficulty?.usually_pass ?? []} />
          <ReportCaseList title="Failed in tested configurations" items={data.case_difficulty?.usually_fail ?? []} />
          <ReportCaseList title="Source failures" items={data.case_difficulty?.frequent_source_failures ?? []} />
          <ReportCaseList title="Forbidden failures" items={data.case_difficulty?.frequent_forbidden_failures ?? []} />
        </div>
      </ReportSection>

      <ReportSection name="05-hardware-summary.png" label="Hardware Summary">
        <h2 className="text-xl font-semibold">Runtime And Hardware</h2>
        <div className="mt-4 grid grid-cols-3 gap-3">
          <ReportMetric label="Operating system" value={String(data.runtime?.operating_system ?? "")} />
          <ReportMetric label="App version" value={String(data.application?.version ?? "")} />
          <ReportMetric label="Embedding" value={`${String(data.embedding?.backend ?? "")}/${String(data.embedding?.model ?? "")}`} />
        </div>
      </ReportSection>

      <ReportSection name="06-per-model-summary.png" label="Per-Model Summary">
        <h2 className="text-xl font-semibold">Per-Model Summary</h2>
        <div className="mt-3 grid grid-cols-3 gap-3">
          {Array.from(new Set(groups.map((group) => group.model))).slice(0, 6).map((model) => {
            const modelGroups = groups.filter((group) => group.model === model);
            const best = [...modelGroups].sort((a, b) => b.latest_run_pass_rate - a.latest_run_pass_rate)[0];
            return (
              <div className="rounded-md border border-ink/15 p-3" key={model}>
                <p className="truncate text-sm font-semibold">{model}</p>
                <p className="mt-1 text-xs text-ink/60">{modelGroups.length} config(s)</p>
                <p className="mt-2 text-xs">Best: {best ? `${best.latest_run_passed}/${best.latest_run_total}` : "n/a"}</p>
              </div>
            );
          })}
          {groups.length === 0 && <p className="text-sm text-ink/55">No model summaries available.</p>}
        </div>
      </ReportSection>
    </div>
  );
}

function ReportSection(props: { name: string; label: string; children: React.ReactNode }) {
  return (
    <section
      className="rounded-md border border-ink/15 bg-white p-5"
      data-report-label={props.label}
      data-report-snapshot={props.name}
    >
      {props.children}
    </section>
  );
}

function ReportMetric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-md border border-ink/15 bg-[#faf9f3] p-3">
      <p className="text-xs font-medium uppercase text-ink/45">{label}</p>
      <p className="mt-1 text-sm font-semibold">{value || "n/a"}</p>
    </div>
  );
}

function ReportCaseList({ title, items }: { title: string; items: BenchmarkCaseDifficultyItem[] }) {
  const lowerTitle = title.toLowerCase();
  return (
    <div className="rounded-md border border-ink/15 p-3">
      <p className="text-sm font-semibold">{title}</p>
      {items.length === 0 ? (
        <p className="mt-2 text-xs text-ink/55">No cases.</p>
      ) : (
        <div className="mt-2 space-y-1">
          {items.slice(0, 4).map((item) => (
            <p className="text-xs" key={item.case_id}>
              {item.case_id}: {caseDifficultyItemText(item, lowerTitle)}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

function caseDifficultyItemText(item: BenchmarkCaseDifficultyItem, title: string): string {
  if (title.includes("source")) {
    return `${item.source_failures} source failure(s), ${formatPercent(item.source_failure_rate)}`;
  }
  if (title.includes("forbidden")) {
    return `${item.forbidden_failures} forbidden failure(s), ${formatPercent(item.forbidden_failure_rate)}`;
  }
  return item.observation_label || `${item.passes}/${item.attempts}`;
}

function BenchmarkRunCard({ run, title }: { run: EvalRun; title: string }) {
  return (
    <div className="rounded-md border border-ink/15 bg-white">
      <div className="flex items-center justify-between gap-3 border-b border-ink/10 px-4 py-3">
        <div>
          <p className="text-sm font-semibold">{title}</p>
          <p className="text-xs text-ink/55">
            {run.model} - {formatBenchmarkRunLabel(run)} - temp {run.temperature.toFixed(2)} - {formatTimestamp(run.created_at)}
          </p>
        </div>
        <span
          className={`rounded-md border px-2 py-1 text-xs font-medium ${
            run.total_failed === 0
              ? "border-moss/20 bg-[#edf7ef] text-moss"
              : "border-clay/25 bg-[#fff3ee] text-clay"
          }`}
        >
          {run.total_passed} passed, {run.total_failed} failed
        </span>
      </div>
      <div className="grid grid-cols-8 gap-2 px-4 py-3 text-xs text-ink/65">
        <Metric label="Average latency" value={`${run.average_latency_ms} ms`} />
        <Metric label="Total runtime" value={`${run.total_runtime_ms} ms`} />
        <Metric label="Suite" value={run.suite_version} />
        <Metric label="Mode" value={formatBenchmarkMode(run.benchmark_mode)} />
        <Metric label="Thinking" value={formatThinkingMode(run.thinking_mode)} />
        <Metric label="Status" value={run.status} />
        <Metric label="Practical" value={formatScoreSummary(run.practical_score)} />
        <Metric label="Adversarial" value={formatScoreSummary(run.adversarial_score)} />
      </div>
      <div className="overflow-auto">
        <table className="w-full min-w-[860px] border-t border-ink/10 text-left text-xs">
          <thead className="bg-[#faf9f3] text-ink/60">
            <tr>
              <th className="px-4 py-2 font-medium">Case</th>
              <th className="px-4 py-2 font-medium">Style</th>
              <th className="px-4 py-2 font-medium">Embeddings</th>
              <th className="px-4 py-2 font-medium">Temp</th>
              <th className="px-4 py-2 font-medium">Latency</th>
              <th className="px-4 py-2 font-medium">Status</th>
              <th className="px-4 py-2 font-medium">Expected</th>
              <th className="px-4 py-2 font-medium">Forbidden</th>
              <th className="px-4 py-2 font-medium">Source</th>
              <th className="px-4 py-2 font-medium">Notes</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ink/10">
            {run.cases.map((result) => (
              <Fragment key={result.id}>
                <tr>
                  <td className="px-4 py-2 font-medium">{result.case_id}</td>
                  <td className="px-4 py-2">{formatAnswerStyle(result.answer_style)}</td>
                  <td className="px-4 py-2">{formatCaseEmbedding(result)}</td>
                  <td className="px-4 py-2">{result.temperature.toFixed(2)}</td>
                  <td className="px-4 py-2">{result.latency_ms} ms</td>
                  <td className="px-4 py-2">{result.status}</td>
                  <td className="px-4 py-2">{formatPass(result.expected_passed)}</td>
                  <td className="px-4 py-2">{formatPass(result.forbidden_passed)}</td>
                  <td className="px-4 py-2">{formatPass(result.source_passed)}</td>
                  <td className="px-4 py-2 text-ink/65">
                    {result.reasons.length > 0 ? result.reasons.join("; ") : "ok"}
                  </td>
                </tr>
                <tr className="bg-[#fcfbf7]">
                  <td className="px-4 pb-3 pt-0" colSpan={10}>
                    <details className="rounded-md border border-ink/10 bg-white px-3 py-2">
                      <summary className="cursor-pointer text-xs font-medium text-ink/70">
                        Audit details - {result.pipeline_diagnosis || "no diagnosis"}
                      </summary>
                      <div className="mt-3 grid gap-3 text-xs text-ink/70">
                        <AuditBlock label="Question" value={result.question} />
                        <AuditBlock label="Gold source" value={result.required_source_document} />
                        <AuditBlock label="Prompt" value={result.prompt_text} />
                        <AuditBlock label="Raw answer" value={result.answer_content} />
                        {result.corrected_answer && <AuditBlock label="Corrected answer" value={result.corrected_answer} />}
                        {result.thinking_text && <AuditBlock label="Thinking output" value={result.thinking_text} />}
                        <AuditBlock label="Supplied evidence" value={formatJson(result.supplied_evidence)} />
                        <AuditBlock label="Retrieved candidates" value={formatJson(result.retrieval_candidates)} />
                        <AuditBlock label="Retrieval metrics" value={formatJson(result.retrieval_metrics)} />
                        <AuditBlock label="Grader matches" value={formatJson(result.grader_matches)} />
                        <AuditBlock label="Timings" value={formatJson(result.timings)} />
                      </div>
                    </details>
                  </td>
                </tr>
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BenchmarkComparisonCard({ comparison }: { comparison: BenchmarkComparison }) {
  return (
    <div className="rounded-md border border-ink/15 bg-white">
      <div className="border-b border-ink/10 px-4 py-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <BarChart3 size={17} aria-hidden="true" />
          Benchmark Comparison
        </div>
        <p className="mt-1 text-xs text-ink/55">{comparison.recommendation_reason}</p>
        <p className="mt-1 text-xs text-ink/55">
          Comparing {comparison.included_run_count} current-suite {comparison.comparison_suite_version} run(s).
          {comparison.excluded_run_count > 0
            ? ` ${comparison.excluded_run_count} older/incompatible run(s) excluded: ${comparison.excluded_suite_versions.join(", ")}.`
            : ""}
          {comparison.incomplete_run_count > 0 ? ` ${comparison.incomplete_run_count} partial/incomplete run(s) excluded.` : ""}
        </p>
      </div>
      {comparison.groups.length === 0 ? (
        <p className="px-4 py-6 text-sm text-ink/55">
          No comparable current-suite benchmark runs to compare. Older runs remain in history, but they are excluded from recommendation.
        </p>
      ) : (
        <div className="overflow-auto">
          <table className="w-full min-w-[1080px] text-left text-xs">
            <thead className="bg-[#faf9f3] text-ink/60">
              <tr>
                <th className="px-4 py-2 font-medium">Config</th>
                <th className="px-4 py-2 font-medium">Latest</th>
                <th className="px-4 py-2 font-medium">Best</th>
                <th className="px-4 py-2 font-medium">Avg pass/run</th>
                <th className="px-4 py-2 font-medium">Runs</th>
                <th className="px-4 py-2 font-medium">Avg latency</th>
                <th className="px-4 py-2 font-medium">Verifier</th>
                <th className="px-4 py-2 font-medium">Guidance</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-ink/10">
              {comparison.groups.map((group) => (
                <tr className={group.recommended ? "bg-[#edf7ef]" : ""} key={group.key}>
                  <td className="px-4 py-2">
                    <p className="font-medium">{group.model}</p>
                    <p className="text-ink/55">
                      {formatBenchmarkMode(group.benchmark_mode)} - thinking {formatThinkingMode(group.thinking_mode)}
                    </p>
                    <p className="text-ink/55">
                      {formatGroupEmbedding(group)} - temp {group.temperature.toFixed(2)} - {group.prompt_version || "legacy prompt"}
                    </p>
                    {group.recommended && <p className="mt-1 text-moss">Recommended</p>}
                  </td>
                  <td className="px-4 py-2">
                    <p>{group.latest_run_passed}/{group.latest_run_total}</p>
                    <p className="text-ink/55">{formatPercent(group.latest_run_pass_rate)}</p>
                  </td>
                  <td className="px-4 py-2">
                    <p>{group.best_run_passed}/{group.best_run_total}</p>
                    <p className="text-ink/55">{formatMs(group.best_run_avg_latency_ms)}</p>
                  </td>
                  <td className="px-4 py-2">
                    <p>{group.mean_passed_per_run.toFixed(1)}</p>
                    <p className="text-ink/55">{formatPercent(group.mean_pass_rate)}</p>
                  </td>
                  <td className="px-4 py-2">
                    <p>{group.run_count}</p>
                    <p className="text-ink/55">{group.repeatability_label}</p>
                  </td>
                  <td className="px-4 py-2">
                    <p>{formatMs(group.median_avg_latency_ms)}</p>
                    <p className="text-ink/55">latest {formatMs(group.latest_run_avg_latency_ms)}</p>
                  </td>
                  <td className="px-4 py-2">
                    <p>{group.verify ? "on" : "off"}</p>
                    <p className="text-ink/55">{group.verify ? (group.verifier_recommended ? "worth it" : "not worth latency") : "baseline"}</p>
                  </td>
                  <td className="px-4 py-2">
                    <div className="flex max-w-[280px] flex-wrap gap-1.5">
                      {group.guidance_labels.length === 0 ? (
                        <span className="text-ink/55">No label</span>
                      ) : (
                        group.guidance_labels.map((label) => (
                          <span className="rounded-md border border-ink/10 bg-white px-2 py-1" key={label}>
                            {label}
                          </span>
                        ))
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <CaseDifficultySummary summary={comparison.case_difficulty} />
        </div>
      )}
    </div>
  );
}

function CaseDifficultySummary({ summary }: { summary: BenchmarkCaseDifficulty }) {
  const hasItems =
    summary.usually_pass.length > 0 ||
    summary.usually_fail.length > 0 ||
    summary.frequent_source_failures.length > 0 ||
    summary.frequent_forbidden_failures.length > 0;
  if (!hasItems) return null;
  return (
    <div className="border-t border-ink/10 px-4 py-3">
      <p className="text-sm font-semibold">Case Difficulty</p>
      <p className="mt-1 text-xs text-ink/55">Deterministic summary across saved benchmark runs.</p>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <CaseDifficultyList items={summary.usually_pass} title="Passing cases" />
        <CaseDifficultyList items={summary.usually_fail} title="Failing cases" />
        <CaseDifficultyList items={summary.frequent_source_failures} title="Frequent source failures" />
        <CaseDifficultyList items={summary.frequent_forbidden_failures} title="Frequent forbidden-claim failures" />
      </div>
    </div>
  );
}

function CaseDifficultyList({
  items,
  title,
}: {
  items: BenchmarkCaseDifficulty["usually_pass"];
  title: string;
}) {
  return (
    <div>
      <p className="text-xs font-semibold text-ink/70">{title}</p>
      {items.length === 0 ? (
        <p className="mt-1 text-xs text-ink/45">No cases yet.</p>
      ) : (
        <div className="mt-1 space-y-1.5">
          {items.slice(0, 4).map((item) => (
            <div className="rounded-md border border-ink/10 bg-[#faf9f3] px-2 py-1.5 text-xs" key={item.case_id}>
              <p className="font-medium">{item.case_id}</p>
              <p className="text-ink/55">
                {title.toLowerCase().includes("source")
                  ? `${item.source_failures} source failure(s), ${formatPercent(item.source_failure_rate)}`
                  : title.toLowerCase().includes("forbidden")
                    ? `${item.forbidden_failures} forbidden failure(s), ${formatPercent(item.forbidden_failure_rate)}`
                    : item.observation_label || `${item.passes}/${item.attempts} passed`}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ModelStatsTable({ details }: { details: NonNullable<OllamaStatus["model_details"]> }) {
  return (
    <div className="mt-3 overflow-auto rounded-md border border-ink/10">
      <table className="w-full min-w-[520px] text-left text-xs">
        <thead className="bg-[#faf9f3] text-ink/55">
          <tr>
            <th className="px-3 py-2 font-medium">Model</th>
            <th className="px-3 py-2 font-medium">Params</th>
            <th className="px-3 py-2 font-medium">Quant</th>
            <th className="px-3 py-2 font-medium">Size</th>
            <th className="px-3 py-2 font-medium">Updated</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-ink/10">
          {details.map((model) => (
            <tr key={model.name}>
              <td className="max-w-[190px] truncate px-3 py-2 font-medium" title={model.name}>
                {model.name}
              </td>
              <td className="px-3 py-2">{model.parameter_size || model.family || "unknown"}</td>
              <td className="px-3 py-2">{model.quantization_level || "unknown"}</td>
              <td className="px-3 py-2">{model.size ? formatBytes(model.size) : "unknown"}</td>
              <td className="px-3 py-2">{formatModelModified(model.modified_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DiagnosticRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs font-medium uppercase text-ink/45">{label}</p>
      <p className="mt-0.5 text-sm">{value || "unavailable"}</p>
    </div>
  );
}

function PathRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs font-medium uppercase text-ink/45">{label}</p>
      <p className="mt-0.5 truncate text-sm text-ink/70" title={value}>
        {value || "unavailable"}
      </p>
    </div>
  );
}

const OCR_DEPENDENCIES: Array<[OCRDependencyName, string]> = [
  ["tesseract", "Tesseract"],
  ["pdftoppm", "pdftoppm"],
  ["mutool", "mutool"]
];

function OCRDebugStatus(props: {
  busy: boolean;
  onRefresh: () => void;
  status: OCRStatus | null;
}) {
  const available = Boolean(props.status?.available);
  return (
    <section className="rounded-md border border-ink/15 bg-white p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 text-sm font-semibold">
          {available ? (
            <CheckCircle2 className="text-moss" size={17} aria-hidden="true" />
          ) : (
            <CircleAlert className="text-clay" size={17} aria-hidden="true" />
          )}
          <span>OCR Status</span>
        </div>
        <IconButton disabled={props.busy} onClick={props.onRefresh} title="Refresh OCR status">
          <RefreshCw size={15} aria-hidden="true" />
        </IconButton>
      </div>
      <p className={`text-xs ${available ? "text-moss" : "text-clay"}`}>
        {props.status ? props.status.message : "Checking OCR dependencies..."}
      </p>
      {props.status?.renderer && (
        <p className="mt-1 text-xs text-ink/55">PDF renderer: {props.status.renderer}</p>
      )}
      <div className="mt-3 space-y-2">
        {OCR_DEPENDENCIES.map(([name, label]) => {
          const dependency = props.status?.dependencies?.[name];
          const found = Boolean(dependency?.found);
          return (
            <div className="flex items-start gap-2 rounded-md bg-[#faf9f3] px-2 py-2" key={name}>
              {found ? (
                <CheckCircle2 className="mt-0.5 shrink-0 text-moss" size={14} aria-hidden="true" />
              ) : (
                <CircleAlert className="mt-0.5 shrink-0 text-clay" size={14} aria-hidden="true" />
              )}
              <div className="min-w-0 flex-1">
                <p className="text-xs font-medium">
                  {label} {found ? "found" : "missing"}
                </p>
                <p className="truncate text-xs text-ink/55" title={dependency?.path || ""}>
                  {formatDependencyDetail(dependency)}
                </p>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function OCRResultSummary({ result }: { result: OCRResult }) {
  const ok = Boolean(result.index);
  const warning = result.stats.warning || result.document.ocr_error;
  return (
    <div
      className={`rounded-md border p-4 text-sm ${
        ok ? "border-moss/20 bg-[#edf7ef]" : "border-gold/30 bg-[#fff8e8]"
      }`}
    >
      <div className="flex items-start gap-2">
        {ok ? (
          <CheckCircle2 className="mt-0.5 shrink-0 text-moss" size={16} aria-hidden="true" />
        ) : (
          <AlertTriangle className="mt-0.5 shrink-0 text-[#7a561d]" size={16} aria-hidden="true" />
        )}
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">{result.document.title}</p>
          <p className={`mt-1 text-xs ${ok ? "text-moss" : "text-[#7a561d]"}`}>
            OCR {result.document.ocr_status}
          </p>
          <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-ink/65">
            <Metric label="Pages processed" value={result.stats.pages_processed} />
            <Metric label="Pages with text" value={result.stats.pages_with_text} />
            <Metric label="Chunks" value={result.stats.chunks_created} />
            <Metric label="Embeddings new/cache" value={`${result.stats.embeddings_created}/${result.stats.embeddings_cached}`} />
          </div>
          {warning && <p className="mt-3 text-xs text-[#7a561d]">{warning}</p>}
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-md bg-white/70 px-2 py-2">
      <p className="font-medium text-ink">{value}</p>
      <p className="mt-0.5 text-ink/55">{label}</p>
    </div>
  );
}

function AuditBlock({ label, value }: { label: string; value: string }) {
  if (!value.trim()) return null;
  return (
    <div>
      <p className="mb-1 font-medium text-ink">{label}</p>
      <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-md bg-[#faf9f3] p-2 font-mono text-[11px] leading-5 text-ink/75">
        {value}
      </pre>
    </div>
  );
}

function DocumentRow(props: {
  busy: boolean;
  document: DocumentRecord;
  ocrStatus: OCRStatus | null;
  onDelete: (documentId: string) => void;
  onReindex: (documentId: string) => void;
  onRunOcr: (documentId: string) => void;
}) {
  const lowText = props.document.is_low_text || props.document.index_status === "low_text";
  const canRunOcr = lowText && Boolean(props.ocrStatus?.available);
  const ocrIndexed = props.document.ocr_status === "indexed";
  return (
    <div className="px-4 py-3">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium">{props.document.title}</p>
          <p className="mt-1 truncate text-xs text-ink/55" title={props.document.source_path}>
            {props.document.file_name} · {formatBytes(props.document.size_bytes)}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <StatusPill document={props.document} />
          {canRunOcr && (
            <button
              className="flex h-9 items-center justify-center gap-2 rounded-md border border-moss/20 bg-[#edf7ef] px-3 text-xs font-medium text-moss hover:bg-[#e2f0e5]"
              disabled={props.busy}
              onClick={() => props.onRunOcr(props.document.id)}
              type="button"
            >
              <ScanIcon />
              OCR
            </button>
          )}
          <IconButton
            disabled={props.busy || lowText}
            onClick={() => props.onReindex(props.document.id)}
            title="Reindex document"
          >
            <RotateCw size={15} aria-hidden="true" />
          </IconButton>
          <IconButton
            className="text-clay"
            disabled={props.busy}
            onClick={() => props.onDelete(props.document.id)}
            title="Delete document"
          >
            <Trash2 size={15} aria-hidden="true" />
          </IconButton>
        </div>
      </div>
      {lowText && (
        <div className="mt-3 flex gap-2 rounded-md border border-gold/30 bg-[#fff8e8] px-3 py-2 text-xs text-[#7a561d]">
          <AlertTriangle className="mt-0.5 shrink-0" size={14} aria-hidden="true" />
          <span>
            {props.ocrStatus?.available
              ? `Low-text/scanned PDF detected. OCR is available via ${props.ocrStatus.engine_name}.`
              : props.document.ocr_error || formatOcrUnavailableMessage(props.ocrStatus)}
          </span>
        </div>
      )}
      {ocrIndexed && (
        <div className="mt-3 flex gap-2 rounded-md border border-moss/20 bg-[#edf7ef] px-3 py-2 text-xs text-moss">
          <CheckCircle2 className="mt-0.5 shrink-0" size={14} aria-hidden="true" />
          <span>OCR text indexed with {props.document.ocr_engine || "local OCR"}.</span>
        </div>
      )}
      {props.document.error && !lowText && (
        <p className="mt-2 text-xs text-clay">{props.document.error}</p>
      )}
    </div>
  );
}

function LegacyReportView({ report }: { report: LegacyImportReport }) {
  const sections: Array<[keyof LegacyImportReport, string]> = [
    ["imported", "Imported"],
    ["skipped", "Skipped"],
    ["incompatible", "Incompatible"],
    ["failed", "Failed"]
  ];
  const total = sections.reduce((count, [key]) => count + report[key].length, 0);
  return (
    <div className="rounded-md border border-ink/15 bg-white p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="text-sm font-semibold">Import Report</div>
        <div className="text-xs text-ink/55">{total} item(s)</div>
      </div>
      {report.failed.length > 0 && (
        <div className="mb-3 rounded-md border border-clay/25 bg-[#fff3ee] px-3 py-2 text-xs text-clay">
          Some legacy items could not be imported. The old folder was not changed.
        </div>
      )}
      <div className="space-y-3">
        {sections.map(([key, label]) => (
          <div key={key}>
            <div className={`mb-1 flex items-center justify-between text-xs font-semibold uppercase ${
              key === "failed" && report.failed.length > 0 ? "text-clay" : "text-ink/55"
            }`}>
              <span>{label}</span>
              <span>{report[key].length}</span>
            </div>
            {report[key].length === 0 ? (
              <p className="text-xs text-ink/45">None</p>
            ) : (
              <div className="max-h-32 space-y-1 overflow-auto">
                {report[key].map((item, index) => (
                  <div className="rounded-md bg-[#faf9f3] px-2 py-1 text-xs" key={`${key}-${index}`}>
                    <p className="truncate font-medium">{item.type}: {item.source}</p>
                    <p className="text-ink/55">{item.reason}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function RuntimeStatus({ status }: { status: OllamaStatus | null }) {
  if (!status) {
    return <p className="text-sm text-ink/55">Checking...</p>;
  }

  const ok = status.reachable;
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        {ok ? (
          <CheckCircle2 className="text-moss" size={16} aria-hidden="true" />
        ) : (
          <CircleAlert className="text-clay" size={16} aria-hidden="true" />
        )}
        <span className="text-sm font-medium">{ok ? "Reachable" : "Not reachable"}</span>
      </div>
      <p className="truncate text-xs text-ink/55" title={status.endpoint}>
        {status.endpoint}
      </p>
      <p className="text-xs text-ink/55">
        {status.models.length > 0 ? `${status.models.length} model(s)` : "No models reported"}
      </p>
      {status.error && <p className="text-xs text-clay">{status.error}</p>}
    </div>
  );
}

function DocumentEvidenceCard({ evidence }: { evidence: DocumentEvidenceDiagnostic[] }) {
  if (evidence.length === 0) return null;
  return (
    <div className="max-w-[82%] rounded-md border border-tide/25 bg-[#eef8f8] px-3 py-2 text-xs text-tide">
      <div className="flex items-center gap-2 font-medium">
        <FileText size={14} aria-hidden="true" />
        <span>Document evidence</span>
      </div>
      <div className="mt-2 space-y-2">
        {evidence.map((item) => (
          <details
            className="rounded-md border border-tide/15 bg-white px-2 py-1.5 text-ink/75"
            key={item.document_id}
            open={evidence.length === 1}
          >
            <summary className="cursor-pointer text-xs font-medium text-tide">
              {item.display_name || item.document_id}
            </summary>
            <div className="mt-2 grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
              <Metric label="File type" value={item.file_type || "unknown"} />
              <Metric label="Pages" value={item.page_count ?? 0} />
              <Metric label="Embedded chars" value={item.extracted_text_char_count ?? 0} />
              <Metric label="OCR status" value={documentEvidenceStatusLabel(item)} />
              <Metric label="OCR pages with text" value={item.ocr_pages_with_text ?? 0} />
              <Metric label="OCR chars" value={item.ocr_text_char_count ?? 0} />
              <Metric label="OCR quality" value={item.ocr_quality || "unknown"} />
              <Metric label="OCR attempts" value={item.ocr_attempt_count ?? 0} />
              <Metric label="Crop evidence" value={item.ocr_crop_count ?? 0} />
              <Metric label="VLM-assisted text" value={item.vlm_text_available ? `yes${item.vlm_text_backends?.length ? ` (${item.vlm_text_backends.join(", ")})` : ""}` : "no"} />
              <Metric label="Chunks" value={item.chunk_count ?? 0} />
              <Metric label="Session evidence" value={item.session_attachment_evidence_used ? "yes" : "no"} />
              <Metric label="Indexed Source evidence" value={item.indexed_source_evidence_used ? "yes" : "no"} />
              <Metric label="Sent to model" value={item.model_received_document_evidence ? "yes" : "no"} />
              <Metric label="Retrieved pages" value={formatNumberList(item.retrieved_pages)} />
            </div>
            {(item.ocr_query_terms?.length || item.ocr_exact_matches?.length || item.ocr_fuzzy_matches?.length || item.ocr_context_notes?.length) ? (
              <div className="mt-2 space-y-1 rounded-md bg-[#f7fbfb] px-2 py-2 text-[11px] text-ink/65">
                {item.ocr_query_terms?.length ? (
                  <p><span className="font-medium text-tide">OCR query terms:</span> {item.ocr_query_terms.join(", ")}</p>
                ) : null}
                {item.ocr_exact_matches?.length ? (
                  <p><span className="font-medium text-tide">Exact OCR matches:</span> {item.ocr_exact_matches.join(", ")}</p>
                ) : null}
                {item.ocr_fuzzy_matches?.length ? (
                  <p><span className="font-medium text-tide">Fuzzy OCR matches:</span> {formatFuzzyMatches(item.ocr_fuzzy_matches)}</p>
                ) : null}
                {item.ocr_context_notes?.length ? (
                  <p><span className="font-medium text-tide">OCR context:</span> {item.ocr_context_notes.join(" ")}</p>
                ) : null}
              </div>
            ) : null}
            {item.ocr_line_windows?.length ? (
              <div className="mt-2 space-y-1 rounded-md bg-[#f7fbfb] px-2 py-2 text-[11px] text-ink/65">
                <p className="font-medium text-tide">Retrieved OCR line windows</p>
                {item.ocr_line_windows.slice(0, 3).map((window, index) => (
                  <p className="whitespace-pre-wrap" key={`${window.page}-${window.line_start}-${index}`}>
                    p{window.page ?? "?"} lines {window.line_start ?? "?"}-{window.line_end ?? "?"}: {trimDisplayText(window.text || "", 220)}
                  </p>
                ))}
              </div>
            ) : null}
            {(item.ocr_selected_attempts?.length || item.ocr_crop_evidence?.length) ? (
              <div className="mt-2 space-y-1 rounded-md bg-[#f7fbfb] px-2 py-2 text-[11px] text-ink/65">
                {item.ocr_selected_attempts?.length ? (
                  <p>
                    <span className="font-medium text-tide">Selected OCR attempt:</span>{" "}
                    {formatSelectedOcrAttempt(item.ocr_selected_attempts[0])}
                  </p>
                ) : null}
                {item.ocr_crop_evidence?.length ? (
                  <p>
                    <span className="font-medium text-tide">Crop regions:</span>{" "}
                    {formatCropEvidence(item.ocr_crop_evidence)}
                  </p>
                ) : null}
              </div>
            ) : null}
            {item.evidence_action && (
              <p className="mt-2 text-[11px] text-ink/55">Action: {item.evidence_action}</p>
            )}
            {item.ocr_error && (
              <p className="mt-2 text-[11px] text-clay">{item.ocr_error}</p>
            )}
          </details>
        ))}
      </div>
    </div>
  );
}

function RetrievedSources({
  chunks,
  detailed = false,
  grounding,
  snippets
}: {
  chunks: RAGSearchResult[];
  detailed?: boolean;
  grounding: RAGGroundingReport | null;
  snippets: RAGSnippet[];
}) {
  const sources = summarizeRetrievedSources(chunks);
  const displayedSnippets = snippets.length > 0 ? snippets : chunks.map(chunkToSnippet);
  const unsupportedClaims = grounding?.unsupported_claims ?? [];
  const contradictedClaims = grounding?.contradicted_claims ?? [];
  return (
    <div className="max-w-[82%] rounded-md border border-tide/25 bg-[#eef8f8] px-3 py-2 text-xs text-tide">
      <div className="flex flex-wrap items-center gap-2 font-medium">
        <div className="flex items-center gap-2">
          <FileText size={14} aria-hidden="true" />
          <span>
            Retrieved {displayedSnippets.length} snippet(s) from {chunks.length} chunk(s)
          </span>
        </div>
        <GroundingBadge grounding={grounding} />
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {sources.map((source) => (
          <span
            className="max-w-full truncate rounded-md border border-tide/20 bg-white px-2 py-1"
            key={source}
            title={source}
          >
            {source}
          </span>
        ))}
      </div>
      {(unsupportedClaims.length > 0 || contradictedClaims.length > 0) && (
        <div className="mt-2 rounded-md border border-clay/20 bg-[#fff7f2] px-2 py-1.5 text-clay">
          {contradictedClaims.length > 0 && (
            <p>Possible contradiction detected: {contradictedClaims.join("; ")}</p>
          )}
          {unsupportedClaims.length > 0 && (
            <p>Unsupported claim detected: {unsupportedClaims.join("; ")}</p>
          )}
        </div>
      )}
      {detailed && (
        <div className="mt-2 space-y-1.5">
          {displayedSnippets.map((snippet, index) => (
            <details
              className="rounded-md border border-tide/15 bg-white px-2 py-1.5 text-ink/75"
              key={`${snippet.snippet_id}-${snippet.chunk_id}`}
            >
              <summary className="cursor-pointer text-xs font-medium text-tide">
                {formatRetrievedSnippetLabel(snippet, index)}
              </summary>
              <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-ink/70">
                {snippet.text}
              </p>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}

function GroundingBadge({ grounding }: { grounding: RAGGroundingReport | null }) {
  if (!grounding) return null;
  const verifier = grounding.verifier;
  const label = verifier.enabled
    ? verifier.status === "passed"
      ? "grounding looks okay"
      : verifier.status === "failed"
        ? "grounding needs review"
        : verifier.status === "error"
          ? "grounding check error"
          : "grounding not checked"
    : "verifier off";
  const classes =
    verifier.status === "passed"
      ? "border-moss/20 bg-[#edf7ef] text-moss"
      : verifier.status === "failed" || verifier.status === "error"
        ? "border-clay/25 bg-[#fff3ee] text-clay"
        : "border-tide/20 bg-white text-tide";
  return (
    <span className={`rounded-md border px-2 py-1 text-[11px] font-medium ${classes}`}>
      {label}
      {grounding.regenerated ? " after retry" : ""}
    </span>
  );
}

function StatusPill({ document }: { document: DocumentRecord }) {
  const lowText = document.is_low_text || document.index_status === "low_text";
  const label = documentAttachmentStatusLabel(document);
  const classes = document.ocr_status === "indexed" || document.index_status === "indexed"
      ? "bg-[#edf7ef] text-moss border-moss/20"
      : document.ocr_status === "unavailable" || document.index_status === "error"
        ? "bg-[#fff3ee] text-clay border-clay/25"
        : lowText || document.ocr_status === "no_text"
          ? "bg-[#fff8e8] text-[#7a561d] border-gold/30"
          : "bg-[#eef8f8] text-tide border-tide/25";
  return (
    <span className={`rounded-md border px-2 py-1 text-xs font-medium ${classes}`}>
      {label}
    </span>
  );
}

function ScanIcon() {
  return <Search size={14} aria-hidden="true" />;
}

function MessageBubble({ message }: { message: Message }) {
  const isUser = message.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[82%] whitespace-pre-wrap rounded-md px-3 py-2 text-sm leading-6 ${
          isUser ? "bg-moss text-white" : "border border-ink/15 bg-white text-ink"
        }`}
      >
        {message.content || ((message.artifacts?.length || message.documents?.length) ? "Attachment" : "")}
        <MessageAttachments message={message} />
      </div>
    </div>
  );
}

function IconButton(props: {
  children: React.ReactNode;
  className?: string;
  disabled?: boolean;
  onClick: () => void;
  title: string;
}) {
  return (
    <button
      className={`flex h-9 w-9 items-center justify-center rounded-md border border-ink/15 bg-white hover:bg-[#faf9f3] ${props.className ?? ""}`}
      disabled={props.disabled}
      onClick={props.onClick}
      title={props.title}
      type="button"
    >
      {props.children}
    </button>
  );
}

function BackendDegradedBanner(props: {
  degraded: boolean;
  retrying: boolean;
  onRetry: () => void;
}) {
  const banner = backendBannerState(props.degraded, props.retrying);
  if (!banner.visible) return null;
  return (
    <div
      role="alert"
      className="flex items-center justify-between gap-3 border-b border-clay/30 bg-[#fff3ee] px-5 py-3 text-sm text-clay"
    >
      <span>{banner.message}</span>
      <button
        type="button"
        disabled={banner.retryDisabled}
        onClick={props.onRetry}
        className="shrink-0 rounded border border-clay/40 px-3 py-1 text-xs font-medium text-clay hover:bg-clay/10 disabled:opacity-60"
      >
        {banner.retryLabel}
      </button>
    </div>
  );
}

function ErrorBanner({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className="border-b border-clay/30 bg-[#fff3ee] px-5 py-3 text-sm text-clay">
      {error}
    </div>
  );
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatMs(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "n/a";
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms)} ms`;
}

function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "n/a";
  const totalSeconds = Math.round(ms / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 9) return `${minutes}m`;
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value)) return "n/a";
  return `${Math.round(value * 100)}%`;
}

function formatTimestamp(ms: number): string {
  if (!ms) return "unknown time";
  return new Date(ms).toLocaleString();
}

function formatModelModified(value: string): string {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  return date.toLocaleDateString();
}

function formatAnswerStyle(style: AnswerStyle): string {
  const found = ANSWER_STYLE_OPTIONS.find((option) => option.value === style);
  return found?.label ?? style;
}

function formatPass(value: boolean): string {
  return value ? "pass" : "fail";
}

function benchmarkSummaryMarkdown(runs: EvalRun[]): string {
  if (runs.length === 0) return "";
  const lines = [
    "| Model | Mode | Thinking | Embeddings | Temp | Verify | Status | Passed | Failed | Avg latency | Notes |",
    "| --- | --- | --- | --- | ---: | --- | --- | ---: | ---: | ---: | --- |"
  ];
  for (const run of runs) {
    const failedCases = run.cases
      .filter((result) => !result.passed)
      .map((result) => result.case_id);
    const notes = failedCases.length > 0 ? `failed: ${failedCases.join(", ")}` : "ok";
    lines.push(
      `| ${run.model} | ${formatBenchmarkMode(run.benchmark_mode)} | ${formatThinkingMode(run.thinking_mode)} | ${formatRunEmbedding(run)} | ${run.temperature.toFixed(2)} | ${run.verify ? "on" : "off"} | ${run.status} | ${run.total_passed} | ${run.total_failed} | ${run.average_latency_ms} ms | ${notes} |`
    );
  }
  return lines.join("\n");
}

function imageBenchmarkSummaryMarkdown(runs: ImageEvalRun[]): string {
  if (runs.length === 0) return "";
  const lines = [
    "| Model | Mode | Status | Passed | Failed | Review | Timeout | Runtime errors | Runtime | Notes |",
    "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"
  ];
  for (const run of runs) {
    const failedCases = run.cases
      .filter((result) => !result.passed && !result.grader_review_required)
      .map((result) => result.case_id);
    const notes = failedCases.length > 0 ? `failed: ${failedCases.join(", ")}` : run.notes || "ok";
    lines.push(
      `| ${run.model || "OCR"} | ${run.mode} | ${run.status} | ${run.total_passed} | ${run.total_failed} | ${run.grader_review_count} | ${run.timeout_count} | ${run.runtime_error_count} | ${run.total_runtime_ms} ms | ${notes} |`
    );
  }
  return lines.join("\n");
}

function formatEmbeddingStatus(status?: EmbeddingStatus): string {
  if (!status) return "unknown";
  return status.semantic ? `semantic/${status.model}` : status.model || "lexical";
}

function formatRunEmbedding(run: EvalRun): string {
  if (run.embedding_backend && run.embedding_model) {
    return `${run.embedding_backend}/${run.embedding_model}`;
  }
  return run.embedding_model || run.embedding_backend || "unknown";
}

function formatBenchmarkMode(mode: BenchmarkMode): string {
  if (mode === "retrieval_only") return "Retrieval only";
  if (mode === "oracle_generation") return "Oracle generation";
  return "End-to-end";
}

function formatThinkingMode(mode: ThinkingMode): string {
  if (mode === "legacy/unrecorded") return "legacy/unrecorded";
  return mode;
}

function formatOffload(model: CampaignModelInfo): string {
  if (model.partially_cpu_offloaded) return "partially CPU-offloaded";
  if (model.estimated_gpu_loaded_fraction !== null && model.estimated_gpu_loaded_fraction >= 0.98) {
    return "GPU resident";
  }
  if (model.estimated_cpu_loaded_fraction !== null && model.estimated_cpu_loaded_fraction >= 0.98) {
    return "CPU resident";
  }
  if (model.estimated_gpu_loaded_fraction !== null) {
    return `${Math.round(model.estimated_gpu_loaded_fraction * 100)}% GPU estimate`;
  }
  return "offload unknown";
}

function formatBenchmarkRunLabel(run: EvalRun): string {
  return [
    formatBenchmarkMode(run.benchmark_mode),
    `thinking ${formatThinkingMode(run.thinking_mode)}`,
    `verifier ${run.verify ? "on" : "off"}`,
    formatRunEmbedding(run),
    run.status
  ].join(" - ");
}

function dedupeBenchmarkModes(values: BenchmarkMode[]): BenchmarkMode[] {
  const selected = new Set(values);
  return BENCHMARK_MODE_OPTIONS.map((option) => option.value).filter((value) => selected.has(value));
}

function dedupeThinkingModes(
  values: Array<Exclude<ThinkingMode, "legacy/unrecorded">>
): Array<Exclude<ThinkingMode, "legacy/unrecorded">> {
  const selected = new Set(values);
  return THINKING_MODE_OPTIONS.map((option) => option.value).filter((value) => selected.has(value));
}

function defaultCampaignTitle(preset: CampaignPreset): string {
  const label = CAMPAIGN_PRESET_OPTIONS.find((option) => option.value === preset)?.label ?? "Benchmark campaign";
  const now = new Date();
  return `${label} ${now.toLocaleDateString()} ${now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

function nextFrame(): Promise<void> {
  return new Promise((resolve) => {
    window.requestAnimationFrame(() => resolve());
  });
}

function shouldAutoGenerateCampaignReport(campaign: BenchmarkCampaign): boolean {
  if (!TERMINAL_CAMPAIGN_STATUSES.has(campaign.status)) return false;
  if (!campaign.auto_generate_report) return false;
  if (campaign.report_paths && Object.keys(campaign.report_paths).length > 0) return false;
  return ["", "pending", "awaiting_capture", "capturing", "generating"].includes(campaign.report_status || "");
}

async function waitForImages(root: HTMLElement): Promise<void> {
  const images = Array.from(root.querySelectorAll("img"));
  await Promise.all(
    images.map((image) => {
      if (image.complete && image.naturalWidth > 0) return Promise.resolve();
      return new Promise<void>((resolve) => {
        const finish = () => resolve();
        image.addEventListener("load", finish, { once: true });
        image.addEventListener("error", finish, { once: true });
        window.setTimeout(finish, 1500);
      });
    })
  );
}

function isUsefulPngDataUrl(dataUrl: string): boolean {
  if (!dataUrl.startsWith("data:image/png;base64,")) return false;
  const encoded = dataUrl.slice("data:image/png;base64,".length);
  return encoded.length > 2800 && encoded.startsWith("iVBOR");
}

function formatScoreSummary(score: Record<string, unknown>): string {
  const passed = Number(score.passed ?? 0);
  const total = Number(score.total ?? 0);
  if (!total) return "n/a";
  return `${passed}/${total}`;
}

function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value ?? "");
  }
}

function formatCaseEmbedding(result: EvalCaseResult): string {
  if (result.embedding_backend && result.embedding_model) {
    return `${result.embedding_backend}/${result.embedding_model}`;
  }
  return result.embedding_model || result.embedding_backend || "unknown";
}

function formatGroupEmbedding(group: BenchmarkComparisonGroup): string {
  if (group.embedding_backend && group.embedding_model) {
    return `${group.embedding_backend}/${group.embedding_model}`;
  }
  return group.embedding_model || group.embedding_backend || "unknown";
}

function formatIndexedWithActiveBackend(health: RAGHealth | null): string {
  if (!health) return "unknown";
  if (health.indexed_documents === 0) return "no indexed documents";
  return `${health.documents_indexed_with_active_backend}/${health.indexed_documents}`;
}

function benchmarkRetrievalMismatch(health: RAGHealth | null, run: EvalRun | null): boolean {
  if (!health || !run) return false;
  const benchmarkSemantic = run.embedding_backend === "semantic";
  if (!benchmarkSemantic) return false;
  return !health.embedding.semantic || !health.user_documents_indexed_with_active_backend;
}

function dedupeStrings(values: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const clean = value.trim();
    if (!clean || seen.has(clean)) continue;
    seen.add(clean);
    result.push(clean);
  }
  return result;
}

function selectInstalledModelOrFallback(model: string, installedTags: string[]): string {
  const requested = String(model || "").trim();
  if (installedTags.length === 0) return requested;
  if (requested && isInstalledModelTag(requested, installedTags)) {
    return installedModelTag(requested, installedTags);
  }
  return installedTags[0] ?? requested;
}

function isSupportedImagePath(path: string): boolean {
  const lower = path.toLowerCase();
  return SUPPORTED_IMAGE_EXTENSIONS.some((extension) => lower.endsWith(extension));
}

function isSupportedDocumentPath(path: string): boolean {
  const lower = path.toLowerCase();
  return SUPPORTED_DOCUMENT_EXTENSIONS.some((extension) => lower.endsWith(extension));
}

function isSupportedSourcePath(path: string): boolean {
  const lower = path.toLowerCase();
  return [...SUPPORTED_DOCUMENT_EXTENSIONS, ...SUPPORTED_IMAGE_EXTENSIONS].some((extension) => lower.endsWith(extension));
}

function sourceSummariesFromImportResult(result: SourceImportResult): SourceSummary[] {
  if ("source" in result) return [result.source];
  return result.sources ?? result.imported.map((item) => item.source);
}

function failedSourceImports(result: SourceImportResult): Array<{ name: string; error: string }> {
  if ("failed" in result) return result.failed;
  return [];
}

function sameSource(a: SourceSummary, b: SourceSummary): boolean {
  return a.backend_kind === b.backend_kind && a.id === b.id;
}

function artifactToSource(artifact: ArtifactRecord): SourceSummary {
  const isScreenshot = artifact.source_kind.startsWith("screenshot");
  return {
    id: artifact.id,
    backend_kind: "artifact",
    source_type: isScreenshot ? "screenshot" : "image",
    scope: artifact.scope ?? "library",
    display_name: artifact.name || "Image",
    mime_type: artifact.mime_type || "image/png",
    size_bytes: artifact.size_bytes,
    created_at: artifact.created_at,
    updated_at: artifact.updated_at,
    processing_status: artifact.status,
    indexing_status: "not_indexed",
    thumbnail_path: artifact.thumbnail_path,
    width: artifact.width,
    height: artifact.height,
    error: artifact.error,
    artifact
  };
}

function fileName(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() || path;
}

function makeRequestId(prefix: string): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}

function summarizeRetrievedSources(chunks: RAGSearchResult[]): string[] {
  const seen = new Set<string>();
  const sources: string[] = [];
  for (const chunk of chunks) {
    const name = String(chunk.metadata.title ?? chunk.metadata.file_name ?? "Document");
    const page = chunk.page_start ? `p. ${chunk.page_start}` : "";
    const label = page ? `${name}, ${page}` : name;
    if (!seen.has(label)) {
      seen.add(label);
      sources.push(label);
    }
  }
  return sources;
}

function formatRetrievedSnippetLabel(snippet: RAGSnippet, index: number): string {
  const name = snippet.source || String(snippet.metadata.title ?? snippet.metadata.file_name ?? "Document");
  const page = snippet.page_start ? `, p. ${snippet.page_start}` : "";
  const snippetId = snippet.snippet_id || `S${index + 1}`;
  return `${snippetId}: ${name}${page}, chunk ${snippet.chunk_id.slice(0, 8)}`;
}

function documentEvidenceStatusLabel(item: DocumentEvidenceDiagnostic): string {
  if (item.ocr_status === "indexed") return "OCR indexed";
  if (item.ocr_status === "running" || item.ocr_status === "indexing" || item.ocr_status === "needed") return "OCR processing";
  if (item.ocr_status === "no_text") return "no reliable text";
  if (item.ocr_status === "unavailable") return "OCR failed";
  return item.ocr_status || item.index_status || "unknown";
}

function formatNumberList(values?: number[]): string {
  if (!values || values.length === 0) return "none";
  return values.join(", ");
}

function formatFuzzyMatches(matches: Array<{ term?: string; matched_text?: string; distance?: number }>): string {
  return matches
    .map((match) => {
      const matched = match.matched_text || "?";
      const term = match.term || "?";
      const distance = typeof match.distance === "number" ? `, d=${match.distance}` : "";
      return `${matched} ~= ${term}${distance}`;
    })
    .join(", ");
}

function formatSelectedOcrAttempt(attempt?: Record<string, unknown>): string {
  if (!attempt) return "unknown";
  const sourceType = String(attempt.source_type || "ocr_text");
  const preprocessing = String(attempt.preprocessing || "default");
  const psm = attempt.psm ? `psm ${attempt.psm}` : "psm unknown";
  const quality = typeof attempt.quality === "object" && attempt.quality
    ? String((attempt.quality as Record<string, unknown>).label || "unknown")
    : "unknown";
  const crop = typeof attempt.crop === "object" && attempt.crop
    ? String((attempt.crop as Record<string, unknown>).name || "")
    : "";
  return [sourceType, crop, preprocessing, psm, quality].filter(Boolean).join(" · ");
}

function formatCropEvidence(crops?: Array<Record<string, unknown>>): string {
  if (!crops || crops.length === 0) return "none";
  return crops.slice(0, 5).map((crop) => {
    const name = String(crop.name || (crop.coordinates as Record<string, unknown> | undefined)?.name || "crop");
    const quality = typeof crop.quality === "object" && crop.quality
      ? String((crop.quality as Record<string, unknown>).label || "unknown")
      : "unknown";
    const chars = Number(crop.text_char_count || 0);
    return `${name} (${quality}, ${chars} chars)`;
  }).join(", ");
}

function trimDisplayText(text: string, maxChars: number): string {
  const normalized = text.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxChars) return normalized;
  return `${normalized.slice(0, Math.max(0, maxChars - 3)).trim()}...`;
}

function chunkToSnippet(chunk: RAGSearchResult, index: number): RAGSnippet {
  return {
    snippet_id: `S${index + 1}`,
    chunk_id: chunk.chunk_id,
    document_id: chunk.document_id,
    source: String(chunk.metadata.title ?? chunk.metadata.file_name ?? "Document"),
    text: chunk.content,
    score: chunk.score,
    page_start: chunk.page_start,
    page_end: chunk.page_end,
    metadata: chunk.metadata
  };
}

function isRagReadyDocument(document: DocumentRecord): boolean {
  return !document.is_deleted && (document.index_status === "indexed" || document.ocr_status === "indexed");
}

function formatOcrUnavailableMessage(status: OCRStatus | null): string {
  const message = status?.message || "This appears scanned/low-text. OCR is not installed/enabled yet.";
  const missing = missingOcrDependencies(status);
  return missing.length > 0 ? `${message} Missing: ${missing.join(", ")}.` : message;
}

function missingOcrDependencies(status: OCRStatus | null): string[] {
  if (!status?.dependencies) return [];
  const missing: string[] = [];
  if (!status.dependencies.tesseract?.found) {
    missing.push("tesseract");
  }
  if (!status.dependencies.pdftoppm?.found && !status.dependencies.mutool?.found) {
    missing.push("pdftoppm or mutool");
  }
  return missing;
}

function formatDependencyDetail(
  dependency: OCRStatus["dependencies"][OCRDependencyName] | undefined
): string {
  if (!dependency) return "checking";
  if (!dependency.found) return "not found";
  const source = dependency.source === "PATH" ? "PATH" : "fallback";
  return `${source}: ${dependency.path}`;
}

function readError(err: unknown): string {
  if (err instanceof Error) {
    return err.message;
  }
  if (typeof err === "string") {
    return err;
  }
  return "Something went wrong.";
}

function normalizeVisionBackend(value: unknown): VisionBackend {
  if (value === "florence2" || value === "ollama" || value === "ocr_only" || value === "automatic") {
    return value;
  }
  return "automatic";
}

function unsupportedDocumentReason(path: string): string | null {
  const lower = path.toLowerCase();
  const supported = SUPPORTED_DOCUMENT_EXTENSIONS.some((extension) => lower.endsWith(extension));
  return supported ? null : "Unsupported file type. Choose a .txt, .md, or extractable .pdf file.";
}

export default App;
