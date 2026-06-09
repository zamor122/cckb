# Project Constitution (Hot Memory)
## resume-tailor — Production Engineering Guide

---

## 1. Architectural Design & Project Structure

### Stack
- **Framework:** Next.js (App Router, TypeScript)
- **Styling:** Tailwind CSS
- **Database & Auth:** Supabase (PostgreSQL + Row Level Security + Supabase Auth)
- **Payments:** Stripe
- **Analytics:** Umami (`trackEventServer()` for server-side events)
- **Runtime:** Node.js (not Edge — Cerebras SDK requires Node.js modules)
- **Deployment:** Vercel (see `vercel.json`)

### Directory Structure
```
src/app/
  api/                    # Next.js API routes
    humanize/stream/      # Core tailoring engine (SSE, 800 lines)
    tailor/               # Simple tailor POST endpoint
    pipeline/             # Named pipeline orchestrator
    mcp/                  # MCP server for IDE integration
    mcp-tools/            # Deterministic analysis tools (8 tools)
    tools/                # Additional pipeline tools (format-validator, ats-simulator, etc.)
    ai-detection/         # AI content detection
    humanize/             # AI humanization endpoint
    resume/               # Resume CRUD operations
    stripe/               # Stripe webhooks & checkout
    admin/                # Admin endpoints
    cron/                 # Scheduled cron jobs
    research-company/     # Company research standalone
    validate-resume/      # Standalone resume validation
  config/                 # App configuration (models, pipelines, pricing)
  components/             # React components
  contexts/               # React contexts (auth, model selection)
  hooks/                  # Custom React hooks
  lib/                    # Third-party lib wrappers (supabase/server, supabase/client)
  prompts/                # LLM prompt templates (tailoring.ts, tailoringSection.ts, etc.)
  services/               # AI provider abstraction layer
  types/                  # TypeScript type definitions
  utils/                  # Utility functions (60+ files)
```

### Key Architectural Patterns
- **Streaming-first:** Core tailoring uses Server-Sent Events (SSE) via `ReadableStream`
- **Modular routes:** Each feature has its own `route.ts` under `/api/`
- **Parallel tool execution:** MCP tools run in parallel before/after AI generation via `executeParallel()`
- **Section-based tailoring:** When resume parser succeeds, summary + each experience block are tailored independently then reassembled
- **Layered fallbacks:** Model fallback chain, section → full-doc fallback, Ollama offline fallback

---

## 2. Key Styling Patterns & UI Design Guidelines

### Tailwind CSS
- Primary styling via Tailwind CSS (configured in `tailwind.config.ts`)
- Custom color tokens and design system in `src/app/globals.css`
- Score display: **cyan** for "before" score, **pink/gradient** for "after" (improvement delta)

### Scoring UI Convention
```
Before Score: cyan (#06b6d4 range)
After Score: pink/gradient (improvement highlight)
Improvement Delta: shown as "+N pts" with color emphasis
```

### Component Library
- Components in `src/app/components/`
- Contexts for auth state (`src/app/contexts/`)
- Custom hooks for resume management, model state (`src/app/hooks/`)

---

## 3. Implementation Conventions

### Auth Pattern (ALL authenticated routes)
```typescript
// Required imports
import { requireAuth, verifyUserIdMatch } from "@/app/utils/auth";

// Usage in route handler
const authResult = await requireAuth(req, { accessToken });
if ("error" in authResult) return 401;
const verifyResult = verifyUserIdMatch(authResult.userId, userId);
if ("error" in verifyResult) return verifyResult.error;
const authenticatedUserId = authResult.userId;
```

### Route Runtime Declaration
```typescript
// Required in ALL API routes with AI or Node.js dependencies
export const runtime = "nodejs";
export const preferredRegion = "auto";
export const maxDuration = 60;  // seconds; Vercel Pro allows up to 300
```

### Error Handling Convention
```typescript
// Rate limit: propagate immediately, return quotaExceeded payload
if (isRateLimitError(error)) {
  return NextResponse.json({ quotaExceeded: true, retryAfter: N }, { status: 200 });
}
// General errors: log + trackEventServer + 500
await trackEventServer("feature_error", { endpoint, error: msg, userId });
return NextResponse.json({ error: "Processing Error", message }, { status: 500 });
```

### Model Key Convention
```
"provider:modelId"   // e.g., "openai:gpt-4o", "cerebras:llama3.1-70b"
```

### Supabase Admin Client
```typescript
import { supabaseAdmin } from "@/app/lib/supabase/server";
// Used server-side for all DB operations that need to bypass RLS
```

### Analytics Tracking
```typescript
import { trackEventServer } from "@/app/utils/umamiServer";
await trackEventServer("event_name", { key: "value" });
```

### JSON Safety in AI Responses
```typescript
// Use json-extractor utilities to safely parse AI output:
import { parseJSONFromText, extractTailoredResumeFromText } from "@/app/utils/json-extractor";
const jsonData = parseJSONFromText<MyType>(result.text);
// Falls back to extractTailoredResumeFromText() if JSON parse fails
```

### Prompt System
All prompt templates are in `src/app/prompts/`:
- `tailoring.ts` — single-document tailoring prompt with baseline/target scores, keyword context
- `tailoringSection.ts` — section-based prompts for summary + experience bullets
- `tailoringPresets.ts` — `buildUserInstructions()` merges prompt preset IDs + custom instructions
- `keyword-extraction.ts`, `jd-interpreter.ts`, `evaluation.ts`, etc. — specialized prompts

### Sanitization Pipeline (always run on tailored output)
```typescript
// In order — all from src/app/utils/
sanitizeResumeForATS()         // atsSanitizer.ts
deduplicateResumeSections()    // resumeSectionDedupe.ts
rewriteParentheticalKeywords() // keywordParenthesesCleaner.ts
validateOrFixEducationBlock()  // educationValidator.ts
sanitizeContactBlock()         // contactBlockSanitizer.ts
buildContactFromOriginal()     // resumeReassemble.ts
replaceContactBlock()          // contactBlockSanitizer.ts
```

### Database Schema (Supabase `resumes` table)
```sql
resumes (
  id uuid,
  user_id uuid,
  session_id text,
  original_content text,
  tailored_content text,
  obfuscated_content text,
  content_map jsonb,
  job_description text,
  match_score jsonb,        -- { before, after, beforeMetrics, afterMetrics, keywordGap }
  improvement_metrics jsonb,-- { quantifiedBulletsAdded, atsKeywordsMatched, activeVoiceConversions, sectionsOptimized }
  free_reveal jsonb,
  job_title text,
  company_name text,
  parent_resume_id uuid,    -- For versioning
  version_number int,
  root_resume_id uuid,
  format_spec jsonb
)
```

### Named Pipelines (src/app/config/pipelines.ts)
```typescript
type PipelineId = "apply_to_job" | "check_resume" | "prepare_interview" | "full_optimization"
// Each pipeline is a PipelineConfig with ordered steps[]
// internalStep: "tailor" -> delegates to /api/humanize/stream
// endpoint: "/api/mcp-tools/..." -> calls that endpoint directly
```

### Testing
- Test framework: **Vitest** (configured in `vitest.config.ts`)
- Tests in `tests/` directory
- Run with `npm test` or `npx vitest`
