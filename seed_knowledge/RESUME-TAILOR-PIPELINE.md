# Feature Spec: RESUME-TAILOR-PIPELINE
## Feature ID: RESUME-TAILOR-PIPELINE
## Feature Name: Core Resume Tailoring Pipeline

---

## Overview

The resume tailoring pipeline is the primary feature of the application. It takes a user's raw resume and a target job description, then produces an AI-tailored resume optimized for ATS scoring and human readability. The pipeline is orchestrated by the `/api/humanize/stream` route and involves three sequential phases: **Pre-Generation (deterministic MCP tools)**, **AI Generation (LLM tailoring)**, and **Post-Generation (validation + scoring)**.

---

## Entry Points

| Layer | File | Description |
|---|---|---|
| API Route (simple) | `src/app/api/tailor/route.ts` | POST endpoint that delegates to `/api/humanize/stream` |
| API Route (pipeline) | `src/app/api/pipeline/route.ts` | Executes a named pipeline (e.g., `full_optimization`) that may include tailoring |
| API Route (core stream) | `src/app/api/humanize/stream/route.ts` | **The core tailoring engine.** SSE streaming route (800 lines). All tailoring logic lives here. |

---

## Request Parameters (to `/api/humanize/stream`)

```typescript
{
  resume: string;              // Raw resume text (required)
  jobDescription: string;      // Target job description (required)
  sessionId?: string;          // For model selection from session
  modelKey?: string;           // Override model (e.g. "openai:gpt-4o")
  userId?: string;             // Authenticated user ID (required for tailoring)
  accessToken?: string;        // Supabase access token for auth
  quickDraft?: boolean;        // Skip slow steps for speed (default: false)
  jobTitle?: string;           // Client-supplied job title hint
  parentResumeId?: string;     // For versioning - links to previous resume
  customInstructions?: string; // User custom prompting instructions
  keywordsToWeave?: string[];  // Force-include specific keywords
  promptPresetIds?: string[];  // IDs of prompt preset templates to apply
}
```

---

## Pipeline Phases

### Phase 1: Auth & Rate Limiting
- **File:** `src/app/api/humanize/stream/route.ts` (lines 147–194)
- Auth is checked via `requireAuth()` from `src/app/utils/auth.ts`
- User ID verified via `verifyUserIdMatch()` 
- Rate limiting via `checkApiRateLimit()` in `src/app/utils/apiRateLimiter.ts`
- Unauthenticated users receive `401` with `requireAuth: true` flag

### Phase 2: Pre-Generation (Parallel MCP Tools — No AI Calls)
**File:** `src/app/api/humanize/stream/route.ts` (lines 237–277)

Four MCP tools run in parallel via `executeParallel()` (`src/app/utils/mcp-tools.ts`):

| Tool | Endpoint | Returns |
|---|---|---|
| Keyword Extractor | `/api/mcp-tools/keyword-extractor` | `criticalKeywords`, `keywords.technical[]`, `keywords.industry[]`, `cleanedJobDescription`, `jobTitle`, `avoidTerms` |
| Resume Parser | `/api/mcp-tools/resume-parser` | `sections[]`, `experience[]`, `education[]`, `skills{}`, `contactInfo`, `summary` |
| Company Research | `/api/mcp-tools/company-research` | `companyName`, `companyInfo.industry`, `jobTitle` |
| Metrics Context | `/api/mcp-tools/metrics-context` | Quantification guidance for bullet improvements |

Also computes a **baseline ATS score** before generation via `/api/mcp-tools/relevancy-scorer`.

### Phase 3: AI Generation (LLM Tailoring)
**File:** `src/app/api/humanize/stream/route.ts` (lines 424–551)

Two paths:

#### Path A: Section-Based Tailoring (preferred)
- Triggered when `parsedResume.experience.length >= 1 && parsedResume.contactInfo && parsedResume.summary`
- **Summary prompt:** `getSummaryTailoringPrompt()` from `src/app/prompts/tailoringSection.ts`
- **Experience bullets prompts:** `getExperienceBulletsPrompt()` per each experience block (runs in parallel)
- **Reassembly:** `reassembleResumeFromSections()` in `src/app/utils/resumeReassemble.ts`
- Falls back to Path B if section-based fails

#### Path B: Single-Document Tailoring (fallback)
- Uses `getTailoringPrompt()` from `src/app/prompts/tailoring.ts`
- Full resume + job description + target score + sorted missing keywords passed as prompt context
- AI returns JSON with `tailoredResume` + `improvementMetrics`
- Falls back to `extractTailoredResumeFromText()` if JSON parse fails

**AI Provider:** `generateWithFallback()` from `src/app/services/model-fallback.ts`
- Tries `selectedModel` first, then `DEFAULT_MODEL`, then configured fallbacks
- Model selected from session via `getModelFromSession()` in `src/app/utils/model-helper.ts`

### Phase 4: Post-Processing (Sanitization Chain)
**File:** `src/app/api/humanize/stream/route.ts` (lines 560–568)

Sequential sanitization pipeline applied to `tailoredResume`:
1. `sanitizeResumeForATS()` — `src/app/utils/atsSanitizer.ts`
2. `deduplicateResumeSections()` — `src/app/utils/resumeSectionDedupe.ts`
3. `rewriteParentheticalKeywords()` — `src/app/utils/keywordParenthesesCleaner.ts`
4. `validateOrFixEducationBlock()` — `src/app/utils/educationValidator.ts`
5. `sanitizeContactBlock()` — `src/app/utils/contactBlockSanitizer.ts`
6. `buildContactFromOriginal()` + `replaceContactBlock()` — preserves original contact info

### Phase 5: Post-Generation Scoring (Parallel)
**File:** `src/app/api/humanize/stream/route.ts` (lines 606–661)

Runs in parallel via `executeParallel()`:
- **Relevancy Scorer:** `/api/mcp-tools/relevancy-scorer` — returns `before`/`after` ATS match scores (0-100), `beforeMetrics`, `afterMetrics`
- **Resume Validator:** `/api/mcp-tools/resume-validator` — checks for hallucination, content integrity, date consistency

Also runs:
- **Format Recommender:** `/api/mcp-tools/format-recommender` — recommends formatting for the industry/role

### Phase 6: Obfuscation & Persistence
**File:** `src/app/api/humanize/stream/route.ts` (lines 676–754)
- `obfuscateResume()` from `src/app/utils/resumeObfuscator.ts` — creates `obfuscatedResume` + `contentMap` + `freeReveal`
- Saves to Supabase `resumes` table with full metadata (versioning via `parentResumeId`)

---

## SSE Events Streamed to Client

| Event | Payload |
|---|---|
| `status` | `{ stage, message, progress: 10..100 }` |
| `section` | `{ index, total, content, sectionName }` — each resume section streamed |
| `complete` | Full result including `tailoredResume`, `matchScore`, `keywordGap`, `validationResult`, `resumeId` |
| `error` | `{ error, canRetry }` |

---

## Named Pipelines (`/api/pipeline`)
**Config file:** `src/app/config/pipelines.ts`

| Pipeline ID | Steps |
|---|---|
| `apply_to_job` | tailor (humanize stream) |
| `check_resume` | resume-validator, format-validator, ats-simulator |
| `prepare_interview` | skills-gap, interview-prep |
| `full_optimization` | ats-simulator → skills-gap → tailor → resume-validator → interview-prep |

---

## Auth Guard Pattern
```typescript
// Pattern used in humanize/stream/route.ts
const authResult = await requireAuth(req, { accessToken });
if ("error" in authResult) return 401;
const verifyResult = verifyUserIdMatch(authResult.userId, userId);
if ("error" in verifyResult) return verifyResult.error;
const authenticatedUserId = authResult.userId;
```
Helper: `src/app/utils/auth.ts`

---

## Key Keyword Logic
- `sortedMissing`: Critical keywords first (used by relevancy-scorer), then importance-sorted JD keywords not in resume
- `MISSING_KEYWORDS_BLOCKLIST`: Set of EEO/boilerplate terms to never suggest
- `computeKeywordGap()`: Returns `foundInResume[]` and `missingKeywords[]` capped at 20 each

---

## Scoring System
- **Baseline score**: ATS score of original resume vs job description (computed before tailoring)
- **Target improvement**: min(20, gap) points above baseline, capped at 100
- **After score**: Final ATS match of tailored resume vs job description
- Score colors in UI: cyan for "before", pink/gradient for "after" (improvement delta)
