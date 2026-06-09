# Feature Spec: AI-PROVIDER
## Feature ID: AI-PROVIDER
## Feature Name: Multi-Provider AI Generation Layer

---

## Overview

The AI provider layer is an abstraction over multiple LLM providers (OpenAI, Anthropic, Gemini, Cerebras, DeepSeek, Groq, Mistral, HuggingFace, OpenRouter). It handles model selection, API key resolution, and retry/fallback logic. All resume tailoring AI calls go through this layer.

---

## Key Files

| File | Role |
|---|---|
| `src/app/services/ai-provider.ts` | Factory pattern: `createProvider()`, `getModelProvider()`, `generateContentWithFallback()` |
| `src/app/services/model-fallback.ts` | `generateWithFallback()` — primary call interface used in stream route |
| `src/app/services/providers/openai.ts` | OpenAI provider impl |
| `src/app/services/providers/anthropic.ts` | Anthropic provider impl |
| `src/app/services/providers/gemini.ts` | Gemini provider impl |
| `src/app/services/providers/cerebras.ts` | Cerebras provider impl (requires Node.js runtime) |
| `src/app/services/providers/deepseek.ts` | DeepSeek provider impl |
| `src/app/services/providers/groq.ts` | Groq provider impl |
| `src/app/services/providers/mistral.ts` | Mistral provider impl |
| `src/app/services/providers/huggingface.ts` | HuggingFace provider impl |
| `src/app/services/providers/openrouter.ts` | OpenRouter provider impl |
| `src/app/services/error-utils.ts` | `isRateLimitError()`, `isModelUnavailableError()` |
| `src/app/config/models.ts` | Model registry: `getModelConfig()`, `parseModelKey()`, `DEFAULT_MODEL` |
| `src/app/config/models-client.ts` | Client-safe model config subset |
| `src/app/utils/model-helper.ts` | `getModelFromSession()` — resolves model + session API keys |

---

## Model Key Format

```
"provider:modelId"
// Examples:
"openai:gpt-4o"
"anthropic:claude-3-5-sonnet-20241022"
"gemini:gemini-1.5-pro"
"cerebras:llama3.1-70b"
"groq:llama-3.1-70b-versatile"
```

---

## Factory Pattern

```typescript
// src/app/services/ai-provider.ts
export function createProvider(provider: string, modelId: string, apiKey?: string): AIProvider {
  switch (provider) {
    case 'openai': return new OpenAIProvider(modelId, apiKey);
    case 'anthropic': return new AnthropicProvider(modelId, apiKey);
    case 'gemini': return new GeminiProvider(modelId, apiKey);
    case 'cerebras': return new CerebrasProvider(modelId, apiKey);
    // ... etc
  }
}
```

---

## API Key Resolution Order

1. `sessionApiKeys[config.apiKeyEnvVar]` — user-supplied BYOK key from session
2. `process.env[config.apiKeyEnvVar]` — server environment variable
3. Falls back to undefined (provider will throw if required)

---

## Fallback Chain

```typescript
// src/app/services/model-fallback.ts
// generateWithFallback() tries:
// 1. selectedModel (from session or request param)
// 2. DEFAULT_MODEL (configured in models.ts)
// 3. Any additional fallbackModels[]
// Rethrows immediately on rate limit errors (no retry)
```

---

## Rate Limit Handling

- `isRateLimitError(error)` from `src/app/services/error-utils.ts` — detects 429 HTTP status
- `AIProviderError` extends Error with `.status`, `.retryAfter`, `.quotaExceeded`
- Rate limit errors propagate immediately through `generateContentWithFallback()` — no fallback on quota
- The stream route catches and returns `{ quotaExceeded: true, retryAfter: N, canRetryImmediately: true }`

---

## Route Runtime Requirement

```typescript
// IMPORTANT: Cerebras SDK requires Node.js modules
export const runtime = 'nodejs';
export const preferredRegion = 'auto';
export const maxDuration = 60;
```
This is declared in `src/app/api/humanize/stream/route.ts`. All AI-heavy routes use `runtime = 'nodejs'`.

---

## AIProvider Interface

```typescript
interface AIProvider {
  generateContent(prompt: string | string[], options?: ModelOptions): Promise<GenerateContentResult>;
}

interface GenerateContentResult {
  text: string;
  usage?: { promptTokens: number, completionTokens: number, totalTokens: number };
}
```
Defined in `src/app/types/model.ts`.
