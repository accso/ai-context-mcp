/**
 * project_config.ts — Pure helpers that compute project-specific values from
 * a ProjectConfig value object. No process.env, no module-level singleton.
 */
import type { ProjectConfig, ProfileInfo, Source } from "./config.js";

export function projectKey(p: ProjectConfig): string   { return p.name; }
export function displayName(p: ProjectConfig): string  { return p.display_name; }
export function profile(p: ProjectConfig): ProfileInfo { return p.profile; }
export function maxTokens(p: ProjectConfig): number    { return p.mcp?.max_context_tokens ?? 8000; }
export function sources(p: ProjectConfig): Source[]    { return p.sources ?? []; }
export function sourcesCount(p: ProjectConfig): number { return (p.sources ?? []).length; }
export function includeExtensions(p: ProjectConfig): string[] {
  return p.indexer?.include_extensions ?? [];
}

export function llmProvider(p: ProjectConfig): string {
  return p.llm?.provider ?? "lmstudio";
}

export function llmBaseUrl(p: ProjectConfig): string {
  const cfg = p.llm ?? {} as ProjectConfig["llm"];
  if (cfg.provider === "lmstudio") return cfg.lmstudio_base_url ?? "http://host.docker.internal:1234/v1";
  if (cfg.provider === "omlx")     return cfg.omlx_base_url     ?? "http://host.docker.internal:8000/v1";
  return (process.env.OLLAMA_URL ?? "http://ai_context_ollama:11434") + "/v1";
}

// Base URL of the shared, mandatory inference service (embeddings +
// re-ranking) — see .ai-context/inference/. Not read from ProjectConfig:
// this is a fixed internal service on the Docker network, wired in via
// the INFERENCE_URL env var by scripts/start.sh (see multi_server.ts) —
// same for every project, never user-configurable (see docs/configuration.md).
export function inferenceUrl(envFallback = process.env.INFERENCE_URL): string {
  return envFallback ?? "http://ai_context_inference:8080";
}

export function rerankerTopN(envFallback = process.env.RERANKER_TOP_N): number {
  return parseInt(envFallback ?? "20", 10);
}

export function rootPath(p: ProjectConfig): string {
  return (p.root_path ?? "").replace(/\\/g, "/");
}
