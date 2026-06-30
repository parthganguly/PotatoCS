import { ModelCapability, OllamaStatus, rpc } from "../tauri";

export type ConversationModelRole = "chat" | "vision" | "embedding" | "unknown";

export type ConversationModelOption = {
  tag: string;
  canonicalTag: string;
  displayName: string;
  role: ConversationModelRole;
  installed: boolean;
  stale: boolean;
  exactTags: string[];
  tooltip: string;
};

export async function listModelCapabilities(refresh = false): Promise<ModelCapability[]> {
  return rpc<ModelCapability[]>("models.capabilities", { refresh });
}

export async function refreshModelCapabilities(): Promise<ModelCapability[]> {
  return rpc<ModelCapability[]>("models.refresh_capabilities");
}

export function visionCapableModels(capabilities: ModelCapability[]): ModelCapability[] {
  return capabilities.filter((capability) => capability.vision === "yes");
}

export function buildConversationModelOptions(
  ollama: OllamaStatus | null,
  capabilities: ModelCapability[]
): ConversationModelOption[] {
  if ((ollama?.conversation_models?.length ?? 0) > 0 && capabilities.length === 0) {
    return (ollama?.conversation_models ?? [])
      .filter((item) => item.role !== "embedding")
      .map((item) => ({
        tag: item.tag,
        canonicalTag: item.canonical_tag,
        displayName: item.display_name,
        role: item.role,
        installed: item.installed,
        stale: item.stale,
        exactTags: item.exact_tags,
        tooltip: item.tooltip || item.tag
      }));
  }
  const installed = ollama?.models ?? [];
  const details = new Map((ollama?.model_details ?? []).map((item) => [canonicalModelTag(item.name), item.name]));
  const capabilitiesByCanonical = new Map<string, ModelCapability>();
  for (const capability of capabilities) {
    capabilitiesByCanonical.set(canonicalModelTag(capability.model), capability);
  }

  const aliasesByCanonical = new Map<string, string[]>();
  for (const tag of installed) {
    const canonical = canonicalModelTag(tag);
    aliasesByCanonical.set(canonical, [...(aliasesByCanonical.get(canonical) ?? []), tag]);
  }

  const options: ConversationModelOption[] = [];
  for (const [canonical, aliases] of aliasesByCanonical.entries()) {
    const tag = preferredExactTag(aliases, details.get(canonical));
    const capability = capabilitiesByCanonical.get(canonical);
    if (!isChatSelectableModel(tag, capability)) continue;
    const role = classifyModelRole(tag, capability);
    options.push({
      tag,
      canonicalTag: canonical,
      displayName: readableModelLabel(tag),
      role,
      installed: true,
      stale: false,
      exactTags: aliases,
      tooltip: aliases.length > 1 ? `Installed tags: ${aliases.join(", ")}` : tag
    });
  }
  return options.sort((left, right) => left.displayName.localeCompare(right.displayName));
}

export function canonicalModelTag(model: string): string {
  const clean = String(model || "").trim().toLowerCase();
  if (!clean) return "";
  return clean.includes(":") ? clean : `${clean}:latest`;
}

export function isInstalledModelTag(model: string, installedTags: string[]): boolean {
  const canonical = canonicalModelTag(model);
  return Boolean(canonical && installedTags.some((tag) => canonicalModelTag(tag) === canonical));
}

export function installedModelTag(model: string, installedTags: string[]): string {
  const canonical = canonicalModelTag(model);
  return installedTags.find((tag) => canonicalModelTag(tag) === canonical) ?? model;
}

export function readableModelLabel(model: string, options: { installed?: boolean } = {}): string {
  const clean = String(model || "").trim();
  if (!clean) return "No model";
  const withoutLatest = clean.replace(/:latest$/i, "");
  const label = withoutLatest
    .replace(/^qwen3-vl$/i, "Qwen3 VL")
    .replace(/^qwen3$/i, "Qwen3")
    .replace(/^llama3\.2$/i, "Llama 3.2")
    .replace(/^nemotron-nano-chat$/i, "Nemotron Nano Chat")
    .split(/[-_:]/)
    .filter(Boolean)
    .map((part) => formatModelPart(part))
    .join(" ");
  return options.installed === false ? `${label} · not installed` : label;
}

export function classifyModelRole(tag: string, capability?: ModelCapability): ConversationModelRole {
  if (capability?.embedding === "yes" && capability.text_generation !== "yes" && capability.vision !== "yes") {
    return "embedding";
  }
  if (capability?.vision === "yes" && capability.text_generation === "yes") return "vision";
  if (capability?.text_generation === "yes") return "chat";
  if (!capability && looksLikeEmbeddingModel(tag)) return "embedding";
  if (capability?.embedding === "yes") return "embedding";
  return "unknown";
}

function isChatSelectableModel(tag: string, capability?: ModelCapability): boolean {
  const role = classifyModelRole(tag, capability);
  if (role === "embedding") return false;
  if (capability?.text_generation === "yes") return true;
  if (capability?.text_generation === "no") return false;
  if (capability?.embedding === "yes" && capability.vision !== "yes") return false;
  return !looksLikeEmbeddingModel(tag);
}

function preferredExactTag(aliases: string[], detailName?: string): string {
  if (detailName && aliases.includes(detailName)) return detailName;
  const latest = aliases.find((tag) => tag.toLowerCase().endsWith(":latest"));
  return latest ?? aliases[0] ?? "";
}

function looksLikeEmbeddingModel(tag: string): boolean {
  const clean = tag.toLowerCase();
  return /\bembed(ding)?\b/.test(clean) || clean.includes("nomic-embed") || clean.includes("bge-");
}

function formatModelPart(part: string): string {
  const upper = part.toUpperCase();
  if (/^\d+(?:\.\d+)?[BMK]$/i.test(part)) return upper;
  if (/^vl$/i.test(part)) return "VL";
  if (/^v\d+(?:\.\d+)?$/i.test(part)) return upper;
  if (/^llama\d+(?:\.\d+)?$/i.test(part)) return part.replace(/^llama/i, "Llama ");
  if (/^openhermes$/i.test(part)) return "OpenHermes";
  return part.charAt(0).toUpperCase() + part.slice(1);
}
