import { defineAgent } from "eve";
import { createOpenAI } from "@ai-sdk/openai";

// Quiver EVE brain — the deep-research agent that replaces tradingagents/.
//
// PROVIDER ROUTING: OpenRouter (https://openrouter.ai/api/v1) — a single OpenAI-
// compatible gateway that fronts the GLM family (and anything else we switch to
// later). One OPENROUTER_API_KEY; models are addressed by OpenRouter slugs
// (e.g. "z-ai/glm-5.2", "z-ai/glm-4.7-flash"). provider-authored model via
// createOpenAI -> .chat() -> the Chat Completions API; NO Vercel AI Gateway.
//
// Two roles (both GLM, picked for their tier — the legacy mixed-provider split is gone):
//   - quick  (GLM 4.7 Flash, tool-calling): analysts + bull/bear + risk debators.
//   - deep   (GLM 5.2, reasoning ON): Research Manager + PM.
// The top-level agent is the deep/PM role; subagents override `model` to the quick role.
//
// THE WALL: this agent NEVER sees trading caps, the broker, buying power, or ref_ids.

const OPENROUTER_API_KEY = process.env.OPENROUTER_API_KEY!;

const or = createOpenAI({
  baseURL: process.env.OPENROUTER_BASE_URL || "https://openrouter.ai/api/v1",
  apiKey: OPENROUTER_API_KEY,
  name: "openrouter",
  // OpenRouter is OpenAI Chat Completions-compatible (not the Responses API).
  compatibility: "compatible",
});

// .chat() -> Chat Completions endpoint (OpenRouter does not implement /responses).
const REASONER_MODEL = or.chat(process.env.QUIVER_REASONER_MODEL || "z-ai/glm-5.2");
export const QUICK_MODEL = or.chat(process.env.QUIVER_CHAT_MODEL || "z-ai/glm-4.7-flash");

export default defineAgent({
  model: REASONER_MODEL,
  // Set verbatim so EVE skips its AI-Gateway context-window metadata lookup
  // (which only exists for Vercel-gateway-routed models).
  modelContextWindowTokens: 128000,
  // Reasoning ON, high effort — mirrors the legacy stack's GLM thinking. The
  // ai-SDK reasoning level is an enum string ("high").
  reasoning: "high",
  modelOptions: {
    // A full decision turn (4 tool calls + 7 report sections + reasoning) is
    // 3000-5000 output tokens. Set a high max_tokens so OpenRouter doesn't cut
    // the generation mid-report (which left the stream without a
    // message.completed -> the reader stalled). GLM 5.2 supports 16k output.
    // providerOptions values must be JsonObjects (nested under the provider slug).
    providerOptions: {
      openrouter: { max_tokens: 16000 },
      reasoning: { effort: "high" },
    },
  },
  compaction: {
    // Per-ticker decision = a short single-turn flow (gather -> debate -> decide);
    // the context window never fills. Disabling also avoids a gateway metadata lookup.
    thresholdPercent: 1.0,
  },
});
