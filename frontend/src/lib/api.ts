export type AssemblyMeta = {
  assembly: string
  ready: boolean
  cache_label?: string
  software_version?: string
  source_assembly?: string | null
  gencode?: string | null
  genebuild?: string | null
  refseq?: string | null
  dbsnp?: string | null
  gnomad_exomes?: string | null
  gnomad_genomes?: string | null
  clinvar?: string | null
  cosmic?: string | null
  buildver?: string
  protocol?: string | null
}

export type EngineInfo = {
  id: string
  name: string
  version: string
  supported_assemblies: string[]
  ready_assemblies: string[]
  ready: boolean
  default_assembly: string
  mode?: string
  disabled_reason?: string
  assemblies_meta?: Record<string, AssemblyMeta>
}

export type TranscriptHit = {
  gene?: string | null
  feature?: string | null
  cdot?: string | null
  protein?: string | null
  preferred?: boolean
  mane?: boolean
  mane_status?: string | null
  source_index?: number
}

export type VariantResult = {
  input: string
  allele?: string | null
  gene?: string | null
  feature?: string | null
  consequence?: string | null
  impact?: string | null
  cdot?: string | null
  protein?: string | null
  biotype?: string | null
  canonical?: string | null
  engine?: string
  /** All isoform hits (ANNOVAR); preferred is also mirrored in top-level fields */
  transcripts?: TranscriptHit[]
  details?: {
    transcript_pick?: { reason?: string; transcript_count?: number }
    [key: string]: unknown
  }
}

export type AnnotateResponse = {
  assembly: string
  engine: string
  results: VariantResult[]
}

export type JobRun = {
  engine: string
  status: "ok" | "error"
  error?: string
  results: VariantResult[]
}

export type AnnotationJob = {
  id: string
  status: "queued" | "running" | "succeeded" | "failed"
  assembly: string
  engines: string[]
  variant_count: number
  variants?: string[] | null
  variants_preview?: string[]
  error?: string | null
  result?: { assembly: string; runs: JobRun[] } | null
  created_at?: string | null
  started_at?: string | null
  finished_at?: string | null
}

export class ApiError extends Error {
  status: number
  code?: string
  retryAfter?: number

  constructor(
    message: string,
    opts: { status: number; code?: string; retryAfter?: number },
  ) {
    super(message)
    this.name = "ApiError"
    this.status = opts.status
    this.code = opts.code
    this.retryAfter = opts.retryAfter
  }
}

const API_KEY_STORAGE = "biotools_api_key"
const SUBMIT_COOLDOWN_MS = 10_000
let lastSuccessfulSubmitAt: number | null = null

export function getApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE) || ""
}

export function setApiKey(key: string) {
  if (key) localStorage.setItem(API_KEY_STORAGE, key)
  else localStorage.removeItem(API_KEY_STORAGE)
}

export function getSubmitCooldownRemainingMs(lastSubmitAt: number | null): number {
  // The page state still triggers re-renders, but only a successful API response
  // is allowed to start the client-side cooldown.
  if (lastSubmitAt == null && lastSuccessfulSubmitAt == null) return 0
  if (!lastSuccessfulSubmitAt) return 0
  return Math.max(
    0,
    SUBMIT_COOLDOWN_MS - (Date.now() - lastSuccessfulSubmitAt),
  )
}

function csrfToken(): string | undefined {
  const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/)
  return match ? decodeURIComponent(match[1]) : undefined
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  }
  const token = csrfToken()
  if (token) headers["X-CSRFToken"] = token
  const apiKey = getApiKey()
  if (apiKey) headers["X-API-Key"] = apiKey
  return headers
}

async function parseError(res: Response): Promise<ApiError> {
  let message = `request failed: ${res.status}`
  let code: string | undefined
  let retryAfter: number | undefined
  try {
    const data = await res.json()
    message = data?.error?.message || data?.detail || message
    code = data?.error?.code
    if (typeof data?.error?.retry_after === "number") {
      retryAfter = data.error.retry_after
    }
  } catch {
    /* ignore */
  }
  const headerRetry = res.headers.get("Retry-After")
  if (retryAfter == null && headerRetry) {
    const n = Number(headerRetry)
    if (!Number.isNaN(n)) retryAfter = n
  }
  return new ApiError(
    typeof message === "string" ? message : JSON.stringify(message),
    { status: res.status, code, retryAfter },
  )
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function isTransientPollError(error: unknown): boolean {
  if (error instanceof ApiError) {
    return error.status === 408 || error.status === 429 || error.status >= 500
  }
  // Browser fetch rejects with TypeError for DNS, connection and CORS failures.
  return error instanceof TypeError
}

export async function fetchEngines(): Promise<EngineInfo[]> {
  const res = await fetch("/api/v1/engines/")
  if (!res.ok) throw await parseError(res)
  const data = (await res.json()) as { engines: EngineInfo[] }
  return data.engines
}

export async function annotateVariants(payload: {
  engine: string
  assembly: string
  variants: string[]
}): Promise<AnnotateResponse> {
  const res = await fetch("/api/v1/annotations/", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw await parseError(res)
  return res.json()
}

export async function createAnnotationJob(payload: {
  engines: string[]
  assembly: string
  variants: string[]
}): Promise<AnnotationJob> {
  const res = await fetch("/api/v1/jobs/", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw await parseError(res)
  const job = (await res.json()) as AnnotationJob
  lastSuccessfulSubmitAt = Date.now()
  return job
}

export async function fetchAnnotationJob(id: string): Promise<AnnotationJob> {
  const res = await fetch(`/api/v1/jobs/${id}/`, { headers: authHeaders() })
  if (!res.ok) throw await parseError(res)
  return res.json()
}

export async function listAnnotationJobs(limit = 50): Promise<AnnotationJob[]> {
  const res = await fetch(`/api/v1/jobs/?limit=${limit}`, {
    headers: authHeaders(),
  })
  if (!res.ok) throw await parseError(res)
  const data = (await res.json()) as { jobs: AnnotationJob[] }
  return data.jobs
}

export async function waitForAnnotationJob(
  id: string,
  opts: {
    intervalMs?: number
    timeoutMs?: number
    maxTransientErrors?: number
  } = {},
): Promise<AnnotationJob> {
  const intervalMs = opts.intervalMs ?? 1500
  const timeoutMs = opts.timeoutMs ?? 320_000
  const maxTransientErrors = opts.maxTransientErrors ?? 3
  const started = Date.now()
  let transientErrors = 0

  while (Date.now() - started < timeoutMs) {
    try {
      const job = await fetchAnnotationJob(id)
      transientErrors = 0
      if (job.status === "succeeded" || job.status === "failed") return job
    } catch (error) {
      if (
        !isTransientPollError(error) ||
        transientErrors >= maxTransientErrors
      ) {
        throw error
      }
      transientErrors += 1
      const backoffMs = Math.min(
        intervalMs * 2 ** (transientErrors - 1),
        10_000,
      )
      await sleep(backoffMs)
      continue
    }
    await sleep(intervalMs)
  }
  throw new ApiError("任务等待超时", { status: 504, code: "job_timeout" })
}

export type KnowledgeMeta = {
  ready?: boolean
  error?: string
  imported_at?: string
  gene_loaded?: number
  transcript_loaded?: number
  genes?: {
    gene_count?: number
    by_type?: Record<string, number>
  }
  mane?: {
    version?: string
    assembly?: string
    assembly_note?: string
    refseq_annotation?: string | null
    ensembl_release?: string | null
    mane_select?: number
    mane_plus_clinical?: number
    accession_keys?: number
    ftp_current?: string
    summary_url?: string
    docs?: { ncbi?: string; ensembl?: string }
  }
}

export type KnowledgeGene = {
  gene_id: number
  symbol: string
  name: string
  type: string
  chromosome?: string | null
  map_location?: string | null
  ensembl_gene?: string | null
  hgnc_id?: string | null
  synonyms?: string[]
}

export type KnowledgeTranscript = {
  accession: string
  gene?: string
  gene_id?: string | null
  ensembl?: string[]
  refseq?: string[]
  mane?: boolean
  mane_status?: string | null
  note?: string
}

export async function fetchKnowledgeMeta(): Promise<KnowledgeMeta> {
  const res = await fetch("/api/v1/knowledge/meta/")
  if (!res.ok) throw await parseError(res)
  return res.json()
}

export type KnowledgeSourceStatus = {
  url: string
  changed: boolean
  content_sha256: string
  last_modified?: string | null
  last_updated_at?: string | null
  version?: string
}

export type KnowledgeUpdateStatus = {
  status: "idle" | "running" | "succeeded" | "failed" | "interrupted"
  message: string
  last_checked_at?: string | null
  started_at?: string
  finished_at?: string
  update_available?: boolean
  published?: boolean
  sources?: Record<string, KnowledgeSourceStatus>
  current: KnowledgeMeta & {
    release?: string
    updates?: { sources: Record<string, KnowledgeSourceStatus> }
  }
  sources_config: Record<string, string>
}

export async function fetchKnowledgeUpdates(signal?: AbortSignal): Promise<KnowledgeUpdateStatus> {
  const res = await fetch("/api/v1/knowledge/updates/", { headers: authHeaders(), signal, cache: "no-store" })
  if (!res.ok) throw await parseError(res)
  return res.json()
}

export async function startKnowledgeUpdate(action: "check" | "update"): Promise<void> {
  const res = await fetch("/api/v1/knowledge/updates/", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify({ action }),
  })
  if (!res.ok) throw await parseError(res)
}

export async function searchKnowledgeGenes(opts: {
  q?: string
  type?: string
  limit?: number
  offset?: number
}): Promise<{ total: number; results: KnowledgeGene[]; offset: number; limit: number; query_resolution?: { requested: string; accession: string; gene: string | null; version_checked: false } }> {
  const params = new URLSearchParams()
  if (opts.q) params.set("q", opts.q)
  if (opts.type) params.set("type", opts.type)
  params.set("limit", String(opts.limit ?? 50))
  params.set("offset", String(opts.offset ?? 0))
  const res = await fetch(`/api/v1/knowledge/genes/?${params}`)
  if (!res.ok) throw await parseError(res)
  return res.json()
}

export async function searchKnowledgeTranscripts(opts: {
  q?: string
  maneOnly?: boolean
  limit?: number
  offset?: number
}): Promise<{
  total: number
  results: KnowledgeTranscript[]
  offset: number
  limit: number
}> {
  const params = new URLSearchParams()
  if (opts.q) params.set("q", opts.q)
  if (opts.maneOnly) params.set("mane_only", "1")
  params.set("limit", String(opts.limit ?? 50))
  params.set("offset", String(opts.offset ?? 0))
  const res = await fetch(`/api/v1/knowledge/transcripts/?${params}`)
  if (!res.ok) throw await parseError(res)
  return res.json()
}
