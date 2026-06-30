import { BarChart3, Clipboard, Crop, Image, RefreshCw, ScanText, ScreenShare, Trash2, Upload } from "lucide-react";
import { ArtifactAnalysisRun, ArtifactDerivation, ArtifactRecord, ModelCapability, MultimodalMode } from "../../tauri";
import { ArtifactAnalysisCard, imageAssetSrc } from "../multimodal-chat/ImageAttachments";

type ImagesWorkspaceProps = {
  artifacts: ArtifactRecord[];
  busy: boolean;
  error: string | null;
  selectedArtifact: ArtifactRecord | null;
  derivations: ArtifactDerivation[];
  analysis: ArtifactAnalysisRun | null;
  mode: MultimodalMode;
  question: string;
  visionModel: string;
  visionModels: ModelCapability[];
  region: { x: number; y: number; width: number; height: number };
  onAnalyze: () => void;
  onChooseImages: () => void;
  onCopyText: (text: string) => void;
  onDelete: (artifactId: string) => void;
  onImportClipboard: () => void;
  onCaptureFullScreen: () => void;
  onCaptureRegion: () => void;
  onIndex: (artifactId: string, derivationId: string) => void;
  onRefresh: () => void;
  onSelectArtifact: (artifactId: string) => void;
  onSetMode: (mode: MultimodalMode) => void;
  onSetQuestion: (value: string) => void;
  onSetRegion: (region: { x: number; y: number; width: number; height: number }) => void;
  onSetVisionModel: (model: string) => void;
  onUnindex: (artifactId: string) => void;
};

export function ImagesWorkspace(props: ImagesWorkspaceProps) {
  const selected = props.selectedArtifact;
  const ocr = latestTextDerivation(props.derivations, "ocr_text");
  const vision = latestTextDerivation(props.derivations, "vision_observations");
  const combined = latestTextDerivation(props.derivations, "combined_evidence");
  const canUseVision = props.mode === "ocr_only" || props.visionModel.trim().length > 0;
  return (
    <>
      <header className="flex h-16 shrink-0 items-center justify-between border-b border-ink/15 px-5">
        <div>
          <h2 className="text-lg font-semibold">Images</h2>
          <p className="text-xs text-ink/55">
            {props.artifacts.length} local image artifact(s), OCR and vision stay profile-local.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            className="flex h-10 items-center gap-2 rounded-md border border-ink/15 bg-white px-3 text-sm font-medium hover:bg-[#faf9f3]"
            disabled={props.busy}
            onClick={props.onRefresh}
            type="button"
          >
            <RefreshCw size={16} aria-hidden="true" />
            Refresh
          </button>
          <button
            className="flex h-10 items-center gap-2 rounded-md bg-moss px-3 text-sm font-medium text-white hover:bg-[#35543d]"
            disabled={props.busy}
            onClick={props.onChooseImages}
            type="button"
          >
            <Upload size={16} aria-hidden="true" />
            Import
          </button>
        </div>
      </header>

      {props.error && <div className="border-b border-clay/30 bg-[#fff3ee] px-5 py-3 text-sm text-clay">{props.error}</div>}

      <div className="scrollbar-thin min-h-0 flex-1 overflow-auto px-5 py-5">
        <div className="grid gap-5 xl:grid-cols-[minmax(320px,0.9fr)_minmax(0,1.2fr)]">
          <section className="space-y-4">
            <div className="rounded-md border border-ink/15 bg-white p-4">
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
                <ScreenShare size={17} aria-hidden="true" />
                Capture
              </div>
              <div className="flex flex-wrap gap-2">
                <button className="rounded-md border border-ink/15 px-3 py-2 text-sm hover:bg-[#faf9f3]" disabled={props.busy} onClick={props.onImportClipboard} type="button">
                  Paste image
                </button>
                <button className="rounded-md border border-ink/15 px-3 py-2 text-sm hover:bg-[#faf9f3]" disabled={props.busy} onClick={props.onCaptureFullScreen} type="button">
                  Full screen
                </button>
              </div>
              <div className="mt-3 grid grid-cols-4 gap-2">
                {(["x", "y", "width", "height"] as const).map((key) => (
                  <label className="text-xs font-medium text-ink/60" key={key}>
                    {key}
                    <input
                      className="mt-1 h-9 w-full rounded-md border border-ink/20 px-2 text-sm outline-none focus:border-tide"
                      min={key === "width" || key === "height" ? 1 : undefined}
                      onChange={(event) =>
                        props.onSetRegion({ ...props.region, [key]: Number(event.target.value) || 0 })
                      }
                      type="number"
                      value={props.region[key]}
                    />
                  </label>
                ))}
              </div>
              <button
                className="mt-3 flex h-10 items-center gap-2 rounded-md border border-ink/15 bg-white px-3 text-sm font-medium hover:bg-[#faf9f3]"
                disabled={props.busy || props.region.width <= 0 || props.region.height <= 0}
                onClick={props.onCaptureRegion}
                type="button"
              >
                <Crop size={16} aria-hidden="true" />
                Capture region
              </button>
            </div>

            <div className="rounded-md border border-ink/15 bg-white">
              <div className="flex items-center gap-2 border-b border-ink/10 px-4 py-3 text-sm font-semibold">
                <Image size={17} aria-hidden="true" />
                Library
              </div>
              {props.artifacts.length === 0 ? (
                <p className="px-4 py-6 text-sm text-ink/55">No images imported yet.</p>
              ) : (
                <div className="grid max-h-[560px] grid-cols-2 gap-3 overflow-auto p-3">
                  {props.artifacts.map((artifact) => (
                    <button
                      className={`rounded-md border p-2 text-left hover:bg-[#faf9f3] ${
                        artifact.id === selected?.id ? "border-moss bg-[#edf7ef]" : "border-ink/15 bg-white"
                      }`}
                      key={artifact.id}
                      onClick={() => props.onSelectArtifact(artifact.id)}
                      type="button"
                    >
                      <img
                        alt=""
                        className="mb-2 h-28 w-full rounded-md border border-ink/10 object-cover"
                        src={imageAssetSrc(artifact.thumbnail_path)}
                      />
                      <p className="truncate text-sm font-medium">{artifact.name}</p>
                      <p className="mt-1 text-xs text-ink/55">
                        {artifact.width}x{artifact.height} - {formatBytes(artifact.size_bytes)}
                      </p>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </section>

          <section className="min-w-0 space-y-4">
            {selected ? (
              <>
                <div className="rounded-md border border-ink/15 bg-white p-4">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <h3 className="truncate text-base font-semibold">{selected.name}</h3>
                      <p className="mt-1 text-xs text-ink/55">
                        {selected.source_kind} - {selected.mime_type} - {selected.width}x{selected.height} - {formatBytes(selected.size_bytes)}
                      </p>
                    </div>
                    <button
                      className="flex h-9 items-center gap-2 rounded-md border border-clay/25 px-3 text-sm text-clay hover:bg-[#fff3ee]"
                      disabled={props.busy}
                      onClick={() => props.onDelete(selected.id)}
                      type="button"
                    >
                      <Trash2 size={15} aria-hidden="true" />
                      Delete
                    </button>
                  </div>
                  <img
                    alt=""
                    className="mt-4 max-h-[360px] w-full rounded-md border border-ink/10 object-contain"
                    src={imageAssetSrc(selected.thumbnail_path)}
                  />
                </div>

                <div className="rounded-md border border-ink/15 bg-white p-4">
                  <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
                    <ScanText size={17} aria-hidden="true" />
                    Analyze
                  </div>
                  <div className="grid gap-3 lg:grid-cols-[140px_minmax(160px,1fr)]">
                    <select
                      className="h-10 rounded-md border border-ink/20 bg-white px-3 text-sm outline-none focus:border-tide"
                      disabled={props.busy}
                      onChange={(event) => props.onSetMode(event.target.value as MultimodalMode)}
                      value={props.mode}
                    >
                      <option value="ocr_only">OCR only</option>
                      <option value="vision_only">Vision only</option>
                      <option value="combined">Combined</option>
                    </select>
                    {props.mode === "ocr_only" ? (
                      <p className="self-center text-xs text-ink/55">Uses local OCR only; no Ollama vision model required.</p>
                    ) : (
                      <select
                        className="h-10 rounded-md border border-ink/20 bg-white px-3 text-sm outline-none focus:border-tide"
                        disabled={props.busy || props.visionModels.length === 0}
                        onChange={(event) => props.onSetVisionModel(event.target.value)}
                        value={props.visionModel}
                      >
                        {props.visionModels.length === 0 ? (
                          <option value="">No confirmed vision model</option>
                        ) : (
                          props.visionModels.map((model) => (
                            <option key={model.model} value={model.model}>
                              {model.model}
                            </option>
                          ))
                        )}
                      </select>
                    )}
                  </div>
                  <textarea
                    className="mt-3 min-h-20 w-full rounded-md border border-ink/20 px-3 py-2 text-sm outline-none focus:border-tide"
                    onChange={(event) => props.onSetQuestion(event.target.value)}
                    placeholder="Ask about this image"
                    value={props.question}
                  />
                  <button
                    className="mt-3 flex h-10 items-center gap-2 rounded-md bg-moss px-3 text-sm font-medium text-white hover:bg-[#35543d]"
                    disabled={props.busy || !canUseVision}
                    onClick={props.onAnalyze}
                    type="button"
                  >
                    <BarChart3 size={16} aria-hidden="true" />
                    Analyze image
                  </button>
                </div>

                {props.analysis && <ArtifactAnalysisCard analysis={props.analysis} />}

                <div className="rounded-md border border-ink/15 bg-white p-4">
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <div className="text-sm font-semibold">Derived Text</div>
                    <button
                      className="rounded-md border border-ink/15 px-3 py-1.5 text-xs font-medium hover:bg-[#faf9f3]"
                      disabled={props.busy}
                      onClick={() => props.onUnindex(selected.id)}
                      type="button"
                    >
                      Remove from RAG
                    </button>
                  </div>
                  <DerivationRow derivation={ocr} label="OCR text" onCopy={props.onCopyText} onIndex={(id) => props.onIndex(selected.id, id)} />
                  <DerivationRow derivation={vision} label="Vision description" onCopy={props.onCopyText} onIndex={(id) => props.onIndex(selected.id, id)} />
                  <DerivationRow derivation={combined} label="Combined evidence" onCopy={props.onCopyText} onIndex={(id) => props.onIndex(selected.id, id)} />
                </div>
              </>
            ) : (
              <div className="rounded-md border border-ink/15 bg-white px-4 py-8 text-center text-sm text-ink/55">
                Select an image to preview, analyze, or index derived text.
              </div>
            )}
          </section>
        </div>
      </div>
    </>
  );
}

function DerivationRow(props: {
  derivation: ArtifactDerivation | null;
  label: string;
  onCopy: (text: string) => void;
  onIndex: (derivationId: string) => void;
}) {
  if (!props.derivation?.text_content?.trim()) {
    return <p className="border-t border-ink/10 py-3 text-sm text-ink/50">{props.label}: none yet</p>;
  }
  return (
    <div className="border-t border-ink/10 py-3">
      <div className="mb-2 flex items-center justify-between gap-3">
        <p className="text-sm font-medium">{props.label}</p>
        <div className="flex gap-2">
          <button className="rounded-md border border-ink/15 px-2 py-1 text-xs hover:bg-[#faf9f3]" onClick={() => props.onCopy(props.derivation!.text_content)} type="button">
            <Clipboard size={13} aria-hidden="true" />
          </button>
          <button className="rounded-md border border-ink/15 px-2 py-1 text-xs hover:bg-[#faf9f3]" onClick={() => props.onIndex(props.derivation!.id)} type="button">
            Index
          </button>
        </div>
      </div>
      <p className="max-h-28 overflow-auto whitespace-pre-wrap rounded-md bg-[#faf9f3] p-2 text-xs leading-5 text-ink/70">
        {props.derivation.text_content}
      </p>
    </div>
  );
}

function latestTextDerivation(derivations: ArtifactDerivation[], kind: ArtifactDerivation["kind"]): ArtifactDerivation | null {
  return [...derivations].reverse().find((derivation) => derivation.kind === kind && Boolean(derivation.text_content)) ?? null;
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
