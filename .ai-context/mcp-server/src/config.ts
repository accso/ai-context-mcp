import fs from "fs";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface Source {
  project: string;
  scope:   string;
  mode:    "l0" | "l0+l1" | "l0+l1+l2";
  when:    string;
  kind:    "local" | "external";
}

export interface ProfileComponent {
  language:     string;
  layer:        "backend" | "frontend" | "mixed";
  framework:    string | null;
  build_system: string | null;
}

export interface ProfileInfo {
  components: ProfileComponent[];
  monorepo:   boolean;
}

export interface ProjectConfig {
  name:         string;
  display_name: string;
  profile:      ProfileInfo;
  root_path:    string;
  mcp:       { port: number; max_context_tokens: number };
  llm:       { provider: string; lmstudio_base_url: string; omlx_base_url?: string; model: string; disable_thinking?: boolean };
  indexer:   { debounce_seconds: number; extra_skip_paths: string[]; include_extensions: string[] };
  sources:   Source[];
  /** Opt-in LSP bridge languages for this project — see docs/lsp-integration.md. */
  lsp?:      { enabled_languages: string[]; idle_timeout_minutes: number };
}

export interface Resolved {
  repo_root:   string;
  config_path: string;
  projects:    Record<string, ProjectConfig>;
}

// ── Loader ────────────────────────────────────────────────────────────────────

let _project: ProjectConfig | null = null;

function load(): ProjectConfig {
  if (_project) return _project;

  const resolvedPath = process.env.RESOLVED_PATH ?? "/app/.resolved.json";
  const pKey         = process.env.PROJECT_KEY    ?? "";

  if (!pKey) {
    throw new Error("PROJECT_KEY env var is not set. Each container needs PROJECT_KEY=<name>.");
  }

  const resolved = JSON.parse(fs.readFileSync(resolvedPath, "utf8")) as Resolved;

  if (!resolved.projects[pKey]) {
    const available = Object.keys(resolved.projects).join(", ");
    throw new Error(`PROJECT_KEY='${pKey}' not found in .resolved.json. Available: ${available}`);
  }

  _project = resolved.projects[pKey];
  return _project;
}

// ── Accessors ─────────────────────────────────────────────────────────────────

export const getConfig      = load;
export const projectKey     = () => load().name;
export const projectName    = projectKey;               // backward-compat alias
export const displayName    = () => load().display_name;
export const profile        = () => load().profile;
export const mcpPort        = () => Number(process.env.PORT ?? load().mcp.port);
export const maxTokens       = () => load().mcp.max_context_tokens ?? 8000;
export const sources        = () => load().sources ?? [];
export const sourcesCount   = () => (load().sources ?? []).length;
export const qdrantUrl      = () => process.env.QDRANT_URL   ?? "http://localhost:6333";
export const pgUrl          = () => process.env.POSTGRES_URL ?? "postgresql://ai_context:ai_context_local@localhost:5433/ai_context";

// File extensions the indexer accepts (safelist). `get_file` uses the same
// list to decide whether a path is servable at all — see README § File-type handling.
export const includeExtensions = () => load().indexer?.include_extensions ?? [];

// Backward-compat: the old config had scopePath; now it's always "" (project_root IS the scope)
export const scopePath      = () => "";

// ── omlx API key (optional) ───────────────────────────────────────────────────
// omlx is a local LLM runtime at 127.0.0.1:8000 (similar to LM Studio).
// No API key is needed for a default local setup.
// If you enable auth on your omlx instance, set OMLX_API_KEY in your shell —
// it is forwarded to containers by start.sh and added as Authorization: Bearer.

export function omlxApiKey(): string | null {
  return process.env.OMLX_API_KEY || null;
}

export function providerHeaders(provider: string): Record<string, string> {
  if (provider === "omlx") {
    const key = omlxApiKey();
    if (key) return { Authorization: `Bearer ${key}` };
  }
  return {};
}

export function llmProvider(): string {
  return load().llm?.provider ?? "lmstudio";
}

export function llmBaseUrl(): string {
  const cfg = load().llm ?? {} as ProjectConfig["llm"];
  if (cfg.provider === "lmstudio") return cfg.lmstudio_base_url ?? "http://host.docker.internal:1234/v1";
  if (cfg.provider === "omlx")     return cfg.omlx_base_url     ?? "http://host.docker.internal:8000/v1";
  return (process.env.OLLAMA_URL ?? "http://ai_context_ollama:11434") + "/v1";
}

export function llmHeaders(): Record<string, string> {
  return providerHeaders(llmProvider());
}

// Base URL of the shared, mandatory inference service (embeddings +
// re-ranking) — see .ai-context/inference/. Not read from config.yaml: a
// fixed internal service on the Docker network, wired in via the
// INFERENCE_URL env var by scripts/start.sh — never user-configurable.
export function inferenceUrl(): string {
  return process.env.INFERENCE_URL ?? "http://ai_context_inference:8080";
}

export function rerankerTopN(): number {
  return parseInt(process.env.RERANKER_TOP_N ?? "20", 10);
}

