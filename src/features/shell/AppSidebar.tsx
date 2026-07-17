import { Bot, MessageSquarePlus, Power, RefreshCw, Save, Settings as SettingsIcon } from "lucide-react";
import { isInstalledModelTag, readableModelLabel } from "../../api/models";
import type { AppStatus, OllamaStatus, Session, Settings, VisionBackend } from "../../tauri";

export { readableModelLabel } from "../../api/models";

type ActiveView = "chat" | "sources" | "storage" | "diagnostics";

const VIEW_LABELS: Record<ActiveView, string> = {
  chat: "Chat",
  sources: "Sources",
  storage: "Storage",
  diagnostics: "Diagnostics"
};

export function AppSidebar(props: {
  activeView: ActiveView;
  appStatus: AppStatus | null;
  busy: boolean;
  modelChoices: string[];
  modelDraft: string;
  ollama: OllamaStatus | null;
  selectedSessionId: string | null;
  sessions: Session[];
  settings: Settings;
  settingsOpen: boolean;
  visionBackendDraft: VisionBackend;
  onCreateSession: () => void;
  onRefreshOllama: () => void;
  onSaveSettings: () => void;
  onSelectSession: (sessionId: string) => void;
  onSetActiveView: (view: ActiveView) => void;
  onSetModelDraft: (model: string) => void;
  onSetVisionBackendDraft: (backend: VisionBackend) => void;
  onToggleSettings: () => void;
}) {
  return (
    <aside className="flex w-[310px] shrink-0 flex-col border-r border-ink/15 bg-[#eeeee6]">
      <div className="border-b border-ink/15 px-4 py-4">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-md bg-moss text-white">
            <Bot size={19} aria-hidden="true" />
          </div>
          <div className="min-w-0">
            <h1 className="truncate text-base font-semibold">Odysseus Desktop</h1>
            <p className="truncate text-xs text-ink/60">Local AI workspace</p>
          </div>
        </div>
        <button
          className="mt-4 flex h-10 w-full items-center justify-center gap-2 rounded-md bg-moss px-3 text-sm font-semibold text-white hover:bg-[#35543d]"
          disabled={props.busy}
          onClick={props.onCreateSession}
          type="button"
        >
          <MessageSquarePlus size={17} aria-hidden="true" />
          New chat
        </button>
      </div>

      <nav className="grid grid-cols-2 gap-2 border-b border-ink/15 px-4 py-3">
        {(["chat", "sources", "storage", "diagnostics"] as ActiveView[]).map((view) => (
          <button
            className={`rounded-md px-2 py-2 text-sm font-medium ${
              props.activeView === view ? "bg-moss text-white" : "bg-white hover:bg-[#faf9f3]"
            }`}
            key={view}
            onClick={() => props.onSetActiveView(view)}
            type="button"
          >
            {VIEW_LABELS[view]}
          </button>
        ))}
      </nav>

      <section className="flex min-h-0 flex-1 flex-col">
        <div className="px-4 py-3">
          <h2 className="text-xs font-semibold uppercase text-ink/55">Recent chats</h2>
        </div>
        <div className="scrollbar-thin min-h-0 flex-1 overflow-auto px-2 pb-3">
          {props.sessions.length === 0 ? (
            <p className="px-2 py-3 text-sm text-ink/55">No recent chats yet.</p>
          ) : (
            props.sessions.map((session) => {
              const sessionModel = session.model || props.settings.default_model || "";
              const installed = isInstalledModelTag(sessionModel, props.modelChoices);
              return (
                <button
                  aria-label={`Open chat ${session.title}`}
                  className={`mb-1 flex w-full flex-col rounded-md px-2 py-2 text-left text-sm ${
                    props.selectedSessionId === session.id ? "bg-moss text-white" : "hover:bg-white"
                  }`}
                  key={session.id}
                  onClick={() => props.onSelectSession(session.id)}
                  title={`${session.title}\n${sessionModel}${installed ? "" : "\nnot installed"}`}
                  type="button"
                >
                  <span className="w-full truncate font-medium">{session.title}</span>
                  <span className={`mt-1 w-full truncate text-xs ${
                    props.selectedSessionId === session.id ? "text-white/70" : "text-ink/45"
                  }`}>
                    {readableModelLabel(sessionModel, { installed })}
                  </span>
                </button>
              );
            })
          )}
        </div>
      </section>

      <footer className="border-t border-ink/15 px-4 py-3">
        <button
          className="flex w-full items-center justify-between rounded-md px-2 py-2 text-left text-sm hover:bg-white"
          onClick={() => props.onSetActiveView("diagnostics")}
          type="button"
        >
          <span className="flex items-center gap-2">
            <Power size={14} aria-hidden="true" />
            Ollama: {props.ollama?.reachable ? "Ready" : "Offline"}
          </span>
          <span className="text-xs text-ink/50">{props.ollama?.models.length ?? 0} models</span>
        </button>
        <div className="mt-1 flex items-center justify-between px-2 py-2 text-sm">
          <span>Profile: {props.appStatus?.profile_id ?? "default"}</span>
          <button
            className="flex h-7 w-7 items-center justify-center rounded-md hover:bg-white"
            onClick={props.onRefreshOllama}
            title="Refresh Ollama"
            type="button"
          >
            <RefreshCw size={14} aria-hidden="true" />
          </button>
        </div>
        <button
          className="mt-1 flex w-full items-center gap-2 rounded-md px-2 py-2 text-sm hover:bg-white"
          onClick={props.onToggleSettings}
          type="button"
        >
          <SettingsIcon size={14} aria-hidden="true" />
          Settings
        </button>
        {props.settingsOpen && (
          <div className="mt-2 rounded-md border border-ink/15 bg-white p-3">
            <label className="text-xs font-semibold text-ink/60" htmlFor="default-model-for-new-chats">
              Default model for new chats
            </label>
            {props.modelChoices.length ? (
              <select
                className="mt-2 w-full rounded-md border border-ink/20 bg-white px-3 py-2 text-sm outline-none focus:border-tide"
                id="default-model-for-new-chats"
                onChange={(event) => props.onSetModelDraft(event.target.value)}
                value={props.modelDraft}
              >
                {!isInstalledModelTag(props.modelDraft, props.modelChoices) && props.modelDraft.trim() && (
                  <option disabled value={props.modelDraft}>
                    {readableModelLabel(props.modelDraft, { installed: false })}
                  </option>
                )}
                {props.modelChoices.map((model) => (
                  <option key={model} title={model} value={model}>
                    {readableModelLabel(model)}
                  </option>
                ))}
              </select>
            ) : props.ollama?.reachable ? (
              <p className="mt-2 rounded-md border border-gold/30 bg-[#fff8e8] px-3 py-2 text-xs text-[#7a561d]">
                No installed chat-capable models were detected.
              </p>
            ) : (
              <input
                className="mt-2 w-full rounded-md border border-ink/20 bg-white px-3 py-2 text-sm outline-none focus:border-tide"
                id="default-model-for-new-chats"
                onChange={(event) => props.onSetModelDraft(event.target.value)}
                placeholder="llama3.2"
                value={props.modelDraft}
              />
            )}
            <label className="mt-3 block text-xs font-semibold text-ink/60" htmlFor="vision-backend">
              Vision backend
            </label>
            <select
              className="mt-2 w-full rounded-md border border-ink/20 bg-white px-3 py-2 text-sm outline-none focus:border-tide"
              id="vision-backend"
              onChange={(event) => props.onSetVisionBackendDraft(event.target.value as VisionBackend)}
              value={props.visionBackendDraft}
            >
              <option value="automatic">Automatic</option>
              <option value="florence2">Basic local vision - Florence 2</option>
              <option value="ollama">Enhanced vision - Ollama</option>
              <option value="ocr_only">OCR only</option>
            </select>
            <button
              className="mt-3 flex h-9 w-full items-center justify-center gap-2 rounded-md bg-tide px-3 text-sm font-medium text-white hover:bg-[#2e5c66]"
              disabled={props.busy}
              onClick={props.onSaveSettings}
              type="button"
            >
              <Save size={15} aria-hidden="true" />
              Save settings
            </button>
          </div>
        )}
      </footer>
    </aside>
  );
}
