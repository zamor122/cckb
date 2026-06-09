# Feature Spec: MCP-TOOLS
## Feature ID: MCP-TOOLS
## Feature Name: Deterministic MCP Tool Suite

---

## Overview

The MCP tool suite is a set of **deterministic, non-AI API endpoints** that perform structured analysis tasks on resumes and job descriptions. They are called in parallel before and after the AI generation step in the tailoring pipeline. Each tool lives in `src/app/api/mcp-tools/<tool-name>/route.ts`.

They are invoked via the `callMCPTool()` and `executeParallel()` utilities in `src/app/utils/mcp-tools.ts`.

---

## Tool Catalog

### 1. Keyword Extractor
**Endpoint:** `POST /api/mcp-tools/keyword-extractor`

**Input:**
```typescript
{ jobDescription: string, resume: string, jobTitle?: string }
```

**Output:**
```typescript
{
  keywords: {
    technical: Array<{ term: string, importance: "critical"|"high"|"medium"|"low", frequency: number, importanceScore: number }>,
    industry: Array<{ term: string, importance: string, frequency: number, importanceScore: number }>
  },
  criticalKeywords: string[],           // Top priority keywords for ATS scoring
  cleanedJobDescription: string,         // HTML-stripped, whitespace-normalized JD
  jobTitle: string,                      // Extracted job title from JD
  avoidTerms: string[],                  // EEO/boilerplate terms to avoid adding
  keywordDensity: Record<string, number> // keyword → occurrence count
}
```

### 2. Resume Parser
**Endpoint:** `POST /api/mcp-tools/resume-parser`

**Input:** `{ resume: string }`

**Output:**
```typescript
{
  sections: string[],
  experience: Array<{
    title: string,
    company: string,
    dates: string | null,
    description: string     // Bullet points text
  }>,
  education: Array<{ institution: string, degree: string, dates: string }>,
  skills: Record<string, string[]>,  // category -> skills list
  contactInfo: { name: string, email: string, phone: string, linkedin: string, location: string } | null,
  summary: string | null
}
```

### 3. Company Research
**Endpoint:** `POST /api/mcp-tools/company-research`

**Input:** `{ jobDescription: string }`

**Output:**
```typescript
{
  companyName: string,
  jobTitle: string,
  companyInfo: {
    industry: string,
    size: string,
    culture: string[],
    techStack: string[]
  }
}
```
Note: `looksLikeCompanyName()` guard applied after — rejects non-company names (e.g. role descriptions).

### 4. Metrics Context
**Endpoint:** `POST /api/mcp-tools/metrics-context`

**Input:** `{ jobDescription: string }`

**Output:** Guidance object for quantification — used by `formatMetricsGuidance()` in prompts to tell the LLM how to quantify bullet points for this specific industry/role.

### 5. Relevancy Scorer
**Endpoint:** `POST /api/mcp-tools/relevancy-scorer`

**Input:**
```typescript
{
  originalResume: string,
  tailoredResume: string,
  jobDescription: string,
  keywords?: object     // Optional pre-extracted keywords for efficiency
}
```

**Output:**
```typescript
{
  before: number,   // ATS match score of original resume (0-100)
  after: number,    // ATS match score of tailored resume (0-100)
  beforeMetrics: {
    criticalKeywords: { matched: number, total: number },
    concreteEvidence: { withEvidence: number, total: number }
  },
  afterMetrics: { ... same shape as beforeMetrics ... }
}
```

### 6. Resume Validator
**Endpoint:** `POST /api/mcp-tools/resume-validator`

**Input:** `{ originalResume: string, tailoredResume: string }`

**Output:** Validation result object — checks for hallucination, date consistency, content integrity between original and tailored versions.

Also used as a standalone pipeline step: `check_resume` pipeline, `full_optimization` pipeline.

### 7. Format Recommender
**Endpoint:** `POST /api/mcp-tools/format-recommender`

**Input:** `{ jobDescription: string, industry: string, jobTitle: string, tailoredResume: string }`

**Output:** `formatSpec` — industry-specific formatting recommendations (section order, font choices, date format, etc.)

---

## Invocation Pattern

```typescript
// From src/app/utils/mcp-tools.ts
const results = await executeParallel([
  { key: "keywords", fn: () => callMCPTool(baseUrl, "/api/mcp-tools/keyword-extractor", { jobDescription, resume }) },
  { key: "parsedResume", fn: () => callMCPTool(baseUrl, "/api/mcp-tools/resume-parser", { resume }) },
  { key: "companyResearch", fn: () => callMCPTool(baseUrl, "/api/mcp-tools/company-research", { jobDescription }) },
  { key: "metricsContext", fn: () => callMCPTool(baseUrl, "/api/mcp-tools/metrics-context", { jobDescription }) },
]);

// With cache key:
callMCPTool(baseUrl, "/api/mcp-tools/relevancy-scorer", body, {
  cacheKey: generateCacheKey("relevancy", `${resume}:${tailoredResume}:${cleanJobDescription}`)
});
```

---

## Additional Tool Endpoints (non-MCP-tools path)

These live under `/api/tools/` and are used in `check_resume` and `prepare_interview` pipelines:

| Endpoint | Purpose |
|---|---|
| `/api/tools/format-validator` | Format/structure validation |
| `/api/tools/ats-simulator` | Parse & readability simulation |
| `/api/tools/skills-gap` | Skills gap analysis vs job description |
| `/api/tools/interview-prep` | Generates interview prep questions |

---

## MCP Server (External — `/api/mcp`)
**Directory:** `src/app/api/mcp/`
- Exposes the app's tools as an MCP server for IDE integration (Claude Desktop, etc.)
- Related: `src/app/api/mcp-tools/` (tool implementations)
