import axios from "axios";

/**
 * embedQuery — embeds a search query via the shared, mandatory inference
 * service (see .ai-context/inference/main.py), not LM Studio/omlx/ollama.
 * inferenceUrl is threaded down from SharedServices (see project_server.ts/
 * multi_server.ts), which reads it once from the INFERENCE_URL env var.
 *
 * input_type: "query" tells the inference service to apply nomic-embed's
 * "search_query: " task prefix (as opposed to "search_document: " for
 * indexed chunks, applied watcher-side) — a raw OpenAI-passthrough to LM
 * Studio/omlx never applied any prefix at all.
 */
export async function embedQuery(text: string, inferenceUrl?: string | null): Promise<number[]> {
  const url = inferenceUrl ?? "http://ai_context_inference:8080";
  const resp = await axios.post(
    `${url}/embeddings`,
    { input: [text], input_type: "query" },
    { timeout: 30000 }
  );
  return resp.data.data[0].embedding as number[];
}
