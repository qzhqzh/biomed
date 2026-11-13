import { useEffect, useMemo, useRef, useState } from "react"
import { Check, History, Loader2 } from "lucide-react"
import { toast } from "sonner"

import { VariantEditor } from "@/components/editors/VariantEditor"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Separator } from "@/components/ui/separator"
import { AppShell } from "@/layouts/AppShell"
import {
  ApiError,
  createAnnotationJob,
  fetchAnnotationJob,
  fetchEngines,
  getSubmitCooldownRemainingMs,
  listAnnotationJobs,
  waitForAnnotationJob,
  type AnnotationJob,
  type AssemblyMeta,
  type EngineInfo,
  type TranscriptHit,
  type VariantResult,
} from "@/lib/api"
import {
  COMPARE_KIND_LABEL,
  compareFieldValues,
  type CompareKind,
} from "@/lib/annotation-knowledge"
import { cn } from "@/lib/utils"
import { useUnsavedChanges } from "@/lib/navigation"

const TOOLS = [
  { id: "vep", label: "VEP", short: "VEP" },
  { id: "annovar", label: "ANNOVAR", short: "ANNOVAR" },
] as const

const GENOMES = [
  { id: "GRCh37", label: "GRCh37", alias: "hg19" },
  { id: "GRCh38", label: "GRCh38", alias: "hg38" },
] as const

type ToolId = (typeof TOOLS)[number]["id"]
type GenomeId = (typeof GENOMES)[number]["id"]

type RunOutcome = {
  tool: ToolId
  genome: GenomeId
  status: "ok" | "error"
  error?: string
  results: VariantResult[]
  meta?: AssemblyMeta
}

const SAMPLE_BY_GENOME: Record<GenomeId, string> = {
  GRCh37: "17:7577120 C>T\n13:32954018 G>A",
  GRCh38: "17:7675088 C>T\n13:32340368 G>A",
}

function genomeLabel(genome: GenomeId): string {
  const g = GENOMES.find((x) => x.id === genome)
  return g ? `${g.label} / ${g.alias}` : genome
}

function toolLabel(tool: ToolId): string {
  return TOOLS.find((x) => x.id === tool)?.short ?? tool
}

function extractVariants(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => {
      const parts = line.split(/\t+|\s+/)
      if (
        parts.length >= 5 &&
        /^(?:chr)?(?:\d+|X|Y|MT)$/i.test(parts[0]) &&
        /^\d+$/.test(parts[1])
      ) {
        const chrom = parts[0].replace(/^chr/i, "")
        return `${chrom}:${parts[1]} ${parts[3]}>${parts[4]}`
      }
      return line
    })
}

function isToolReady(engines: EngineInfo[], tool: ToolId, genome: GenomeId): boolean {
  const eng = engines.find((e) => e.id === tool)
  if (!eng || eng.disabled_reason) return false
  return eng.ready_assemblies.includes(genome)
}

function toolMeta(
  engines: EngineInfo[],
  tool: ToolId,
  genome: GenomeId,
): AssemblyMeta | undefined {
  return engines.find((e) => e.id === tool)?.assemblies_meta?.[genome]
}

function versionSummary(
  engines: EngineInfo[],
  tool: ToolId,
  genome: GenomeId,
): string {
  const eng = engines.find((e) => e.id === tool)
  const meta = toolMeta(engines, tool, genome)
  if (!eng) return "—"
  if (tool === "vep") {
    const parts = [
      `VEP ${meta?.software_version || eng.version}`,
      meta?.cache_label ? `cache ${meta.cache_label}` : null,
      meta?.gencode || null,
      meta?.dbsnp ? `dbSNP ${meta.dbsnp}` : null,
    ].filter(Boolean)
    return parts.join(" · ")
  }
  const parts = [
    `ANNOVAR ${meta?.software_version || eng.version}`,
    meta?.buildver || null,
    meta?.protocol ? `protocol ${meta.protocol}` : null,
  ].filter(Boolean)
  return parts.join(" · ")
}

function resultLookup(results: VariantResult[]): Map<string, VariantResult> {
  const map = new Map<string, VariantResult>()
  for (const row of results) {
    if (!map.has(row.input)) map.set(row.input, row)
  }
  return map
}

function displayValue(value?: string | null): string {
  const v = (value ?? "").trim()
  return v || "—"
}

function pickReasonLabel(reason?: string): string | null {
  if (reason === "mane_select_nm") return "核心 · MANE NM"
  if (reason === "mane_select") return "核心 · MANE Select"
  if (reason === "mane_plus_clinical_nm") return "核心 · MANE Plus Clinical NM"
  if (reason === "mane_plus_clinical") return "核心 · MANE Plus Clinical"
  if (reason === "canonical") return "核心 · Canonical"
  if (reason === "first_listed") return "核心"
  return null
}

function maneBadge(t: TranscriptHit): string | null {
  if (!t.mane) return null
  return t.mane_status === "plus_clinical" ? "Plus Clinical" : "MANE"
}

function FieldPrimaryCell({
  value,
  row,
  fieldKey,
}: {
  value?: string | null
  row?: VariantResult
  fieldKey: "gene" | "feature" | "cdot" | "protein" | "consequence"
}) {
  const preferredBadge =
    fieldKey === "feature" && row?.canonical === "YES"
      ? pickReasonLabel(row.details?.transcript_pick?.reason) || "主要"
      : null

  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-1.5">
        <span>{displayValue(value)}</span>
        {preferredBadge ? (
          <Badge variant="secondary" className="font-sans text-[10px]">
            {preferredBadge}
          </Badge>
        ) : null}
      </div>
    </div>
  )
}

function AllTranscriptsPanel({
  runs,
  input,
  runMaps,
}: {
  runs: RunOutcome[]
  input: string
  runMaps: Map<ToolId, Map<string, VariantResult>>
}) {
  const panels = runs
    .map((run) => {
      const row = runMaps.get(run.tool)?.get(input)
      const txs = row?.transcripts || []
      if (txs.length <= 1) return null
      return { run, row, txs }
    })
    .filter(Boolean) as Array<{
    run: RunOutcome
    row?: VariantResult
    txs: TranscriptHit[]
  }>

  if (!panels.length) return null

  return (
    <div className="mt-4 space-y-3">
      {panels.map(({ run, row, txs }) => (
        <div key={run.tool} className="rounded-lg border bg-muted/20 p-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <div className="text-sm font-medium">
              {toolLabel(run.tool)} · 全部转录本（{txs.length}）
            </div>
            <span className="text-xs text-muted-foreground">
              上方对比用核心转录本（优先 MANE）；下表为排查用中间信息
              {pickReasonLabel(row?.details?.transcript_pick?.reason)
                ? ` · 核心：${pickReasonLabel(row?.details?.transcript_pick?.reason)}`
                : ""}
            </span>
          </div>
          <div className="overflow-x-auto rounded-md border bg-background">
            <table className="w-full min-w-[28rem] border-collapse text-xs">
              <thead>
                <tr className="border-b text-muted-foreground">
                  <th className="px-2 py-1.5 text-left font-medium">角色</th>
                  <th className="px-2 py-1.5 text-left font-medium">NM / 转录本</th>
                  <th className="px-2 py-1.5 text-left font-medium">c.</th>
                  <th className="px-2 py-1.5 text-left font-medium">p.</th>
                  <th className="px-2 py-1.5 text-left font-medium">标记</th>
                </tr>
              </thead>
              <tbody>
                {txs.map((t) => (
                  <tr
                    key={`${t.feature}-${t.source_index}`}
                    className={cn(
                      "border-b last:border-0",
                      t.preferred && "bg-emerald-50/60 dark:bg-emerald-950/20",
                    )}
                  >
                    <td className="px-2 py-1.5 align-top">
                      {t.preferred ? (
                        <Badge className="text-[10px]">核心</Badge>
                      ) : (
                        <span className="text-muted-foreground">辅助</span>
                      )}
                    </td>
                    <td className="px-2 py-1.5 align-top font-mono">
                      {displayValue(t.feature)}
                    </td>
                    <td className="px-2 py-1.5 align-top font-mono break-all">
                      {displayValue(t.cdot)}
                    </td>
                    <td className="px-2 py-1.5 align-top font-mono break-all">
                      {displayValue(t.protein)}
                    </td>
                    <td className="px-2 py-1.5 align-top">
                      {maneBadge(t) ? (
                        <Badge variant="outline" className="text-[10px]">
                          {maneBadge(t)}
                        </Badge>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  )
}

function compareKindClass(kind: CompareKind): string {
  if (kind === "conflict") return "bg-amber-50/80 dark:bg-amber-950/20"
  if (kind === "isoform") return "bg-sky-50/80 dark:bg-sky-950/20"
  if (kind === "equivalent") return "bg-emerald-50/50 dark:bg-emerald-950/10"
  return ""
}

function compareKindBadgeClass(kind: CompareKind): string {
  if (kind === "conflict") return "text-amber-700 dark:text-amber-400"
  if (kind === "isoform") return "text-sky-700 dark:text-sky-400"
  if (kind === "equivalent") return "text-emerald-700 dark:text-emerald-400"
  return "text-muted-foreground"
}

const COMPARE_FIELDS: Array<{
  key: "gene" | "feature" | "cdot" | "protein" | "consequence"
  label: string
}> = [
  { key: "gene", label: "Gene" },
  { key: "feature", label: "Transcript" },
  { key: "cdot", label: "c." },
  { key: "protein", label: "p." },
  { key: "consequence", label: "效应" },
]

function isGenomeId(value: string): value is GenomeId {
  return value === "GRCh37" || value === "GRCh38"
}

function isToolId(value: string): value is ToolId {
  return value === "vep" || value === "annovar"
}

function jobToOutcomes(job: AnnotationJob, engines: EngineInfo[]): RunOutcome[] {
  const genome = isGenomeId(job.assembly) ? job.assembly : "GRCh37"
  const order = (job.engines || []).filter(isToolId)
  const outcomes = (job.result?.runs || []).map((run) => {
    const tool = isToolId(run.engine) ? run.engine : "vep"
    return {
      tool,
      genome,
      status: (run.status === "ok" ? "ok" : "error") as "ok" | "error",
      error: run.error,
      results: run.results || [],
      meta: toolMeta(engines, tool, genome),
    }
  })
  outcomes.sort((a, b) => {
    const ia = order.indexOf(a.tool)
    const ib = order.indexOf(b.tool)
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib)
  })
  return outcomes
}

function formatJobTime(iso?: string | null): string {
  if (!iso) return ""
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

export default function AnnotateApp() {
  const [engines, setEngines] = useState<EngineInfo[]>([])
  const [genome, setGenome] = useState<GenomeId>("GRCh37")
  const [tools, setTools] = useState<Set<ToolId>>(() => new Set<ToolId>(["vep"]))
  const [text, setText] = useState(SAMPLE_BY_GENOME.GRCh37)
  const [savedText, setSavedText] = useState(SAMPLE_BY_GENOME.GRCh37)
  /** True only while createAnnotationJob request is in flight (not while waiting for result). */
  const [submitting, setSubmitting] = useState(false)
  /** Job id currently being polled for results (may run past the 10s submit cooldown). */
  const [waitingJobId, setWaitingJobId] = useState<string | null>(null)
  const [loadingHistory, setLoadingHistory] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [runs, setRuns] = useState<RunOutcome[]>([])
  const [locusIndex, setLocusIndex] = useState(0)
  const [submittedVariants, setSubmittedVariants] = useState<string[]>([])
  const [loadedJobId, setLoadedJobId] = useState<string | null>(null)
  const [history, setHistory] = useState<AnnotationJob[]>([])
  const [lastSubmitAt, setLastSubmitAt] = useState<number | null>(null)
  const [cooldownLeft, setCooldownLeft] = useState(0)
  const resultsRef = useRef<HTMLDivElement | null>(null)
  const waitingJobIdRef = useRef<string | null>(null)
  const pollingController = useRef(new AbortController())
  useUnsavedChanges(text !== savedText || submitting, "变异注释还有未提交的输入。")

  useEffect(() => {
    pollingController.current = new AbortController()
    return () => { pollingController.current.abort(); waitingJobIdRef.current = null }
  }, [])

  function resetText(value: string) {
    setText(value)
    setSavedText(value)
  }

  const busyWaiting = Boolean(waitingJobId)

  async function refreshHistory() {
    try {
      const jobs = await listAnnotationJobs(20)
      setHistory(jobs.filter((j) => j.status === "succeeded"))
    } catch {
      /* history is optional on this page */
    }
  }

  function applyJobDetail(job: AnnotationJob, engineList: EngineInfo[]) {
    const nextGenome = isGenomeId(job.assembly) ? job.assembly : "GRCh37"
    const nextTools = (job.engines || []).filter(isToolId)
    const variants = job.variants?.length
      ? job.variants
      : job.variants_preview || []
    const outcomes = jobToOutcomes(job, engineList)

    setGenome(nextGenome)
    if (nextTools.length) setTools(new Set(nextTools))
    if (variants.length) {
      resetText(variants.join("\n"))
      setSubmittedVariants(variants)
    }
    setRuns(outcomes)
    setLocusIndex(0)
    setLoadedJobId(job.id)
    setError(null)

    window.requestAnimationFrame(() => {
      resultsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })
    })
  }

  useEffect(() => {
    fetchEngines()
      .then((list) => {
        setEngines(list)
        const defaultGenome: GenomeId = "GRCh37"
        const preferredTools: ToolId[] = ["vep", "annovar"]
        const readyTools = preferredTools.filter((t) =>
          isToolReady(list, t, defaultGenome),
        )
        if (readyTools.length) {
          setTools(new Set([readyTools[0]]))
        }
        setGenome(defaultGenome)
        resetText(SAMPLE_BY_GENOME[defaultGenome])
      })
      .catch((err: Error) => setError(err.message))
    void refreshHistory()
  }, [])

  useEffect(() => {
    if (!lastSubmitAt) {
      setCooldownLeft(0)
      return
    }
    const tick = () => {
      const ms = getSubmitCooldownRemainingMs(lastSubmitAt)
      setCooldownLeft(ms > 0 ? Math.ceil(ms / 1000) : 0)
    }
    tick()
    const id = window.setInterval(tick, 200)
    return () => window.clearInterval(id)
  }, [lastSubmitAt])

  const selectedTools = useMemo(
    () => TOOLS.map((t) => t.id).filter((id) => tools.has(id)),
    [tools],
  )

  const compareMode = selectedTools.length > 1

  function changeGenome(next: GenomeId) {
    if (next === genome) return
    setGenome(next)
    resetText(SAMPLE_BY_GENOME[next])
    setRuns([])
    setSubmittedVariants([])
    setLocusIndex(0)
    // Drop tools that are not ready on the new genome; keep at least one if possible
    setTools((prev) => {
      const kept = [...prev].filter((t) => isToolReady(engines, t, next))
      if (kept.length) return new Set(kept)
      const fallback = TOOLS.map((t) => t.id).find((t) => isToolReady(engines, t, next))
      return fallback ? new Set<ToolId>([fallback]) : new Set()
    })
  }

  function toggleTool(tool: ToolId) {
    if (!isToolReady(engines, tool, genome)) return
    setTools((prev) => {
      const next = new Set(prev)
      if (next.has(tool)) {
        if (next.size === 1) return prev
        next.delete(tool)
      } else {
        next.add(tool)
      }
      return next
    })
  }

  function loadSample() {
    resetText(SAMPLE_BY_GENOME[genome])
  }

  async function loadHistoryJob(jobId: string) {
    setLoadingHistory(true)
    setError(null)
    try {
      const detail = await fetchAnnotationJob(jobId)
      if (detail.status !== "succeeded" || !detail.result?.runs?.length) {
        throw new Error("该任务没有可展示的成功结果")
      }
      applyJobDetail(detail, engines)
      toast.success("已载入历史注释结果")
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setError(message)
      toast.error(message)
    } finally {
      setLoadingHistory(false)
    }
  }

  async function onSubmit() {
    const remainMs = getSubmitCooldownRemainingMs(lastSubmitAt)
    if (remainMs > 0) {
      const sec = Math.ceil(remainMs / 1000)
      const msg = `提交过于频繁，请 ${sec} 秒后再试`
      setError(msg)
      toast.message(msg)
      return
    }

    const variants = extractVariants(text)
    if (!variants.length) {
      setError("请至少输入一条变异记录")
      return
    }
    if (!selectedTools.length) {
      setError("请至少选择一种注释工具")
      return
    }

    const runnable = selectedTools.filter((tool) => isToolReady(engines, tool, genome))
    if (!runnable.length) {
      setError(`所选工具在 ${genomeLabel(genome)} 下均未就绪`)
      return
    }

    setSubmitting(true)
    setError(null)
    setLastSubmitAt(Date.now())
    let createdId: string | null = null
    try {
      const created = await createAnnotationJob({
        engines: runnable,
        assembly: genome,
        variants,
      })
      createdId = created.id
      setSavedText(text)
      waitingJobIdRef.current = created.id
      setWaitingJobId(created.id)
      toast.message("任务已提交，正在后台运行…")
    } catch (err) {
      if (err instanceof ApiError && err.code === "submit_too_soon") {
        const sec = err.retryAfter ?? 10
        const msg = err.message || `请等待 ${sec} 秒后再提交`
        setError(msg)
        toast.message(msg)
      } else {
        const message = err instanceof Error ? err.message : String(err)
        setError(message)
        toast.error(message)
      }
      return
    } finally {
      // Unlock submit after create; 10s cooldown still applies via cooldownLeft.
      setSubmitting(false)
    }

    if (!createdId) return
    const jobId = createdId
    try {
      const finished = await waitForAnnotationJob(jobId, { signal: pollingController.current.signal })
      // Ignore stale completion if user already submitted a newer job.
      if (waitingJobIdRef.current !== jobId) return

      const detail = await fetchAnnotationJob(finished.id)
      if (waitingJobIdRef.current !== jobId) return

      if (!detail.result?.runs?.length && detail.status === "succeeded") {
        throw new Error("任务已完成，但未返回注释明细，请从下方历史记录重新载入")
      }
      applyJobDetail(detail, engines)
      void refreshHistory()

      const outcomes = jobToOutcomes(detail, engines)
      const ok = outcomes.filter((r) => r.status === "ok").length
      const fail = outcomes.length - ok
      if (detail.status === "failed" || ok === 0) {
        setError(detail.error || "任务失败")
        toast.error(detail.error || "任务失败")
      } else if (fail) {
        toast.message(`完成 ${ok} 种，失败 ${fail} 种`)
      } else if (outcomes.length > 1) {
        toast.success(`完成 ${ok} 种工具对比（${genomeLabel(detail.assembly as GenomeId)}）`)
      } else {
        toast.success("注释完成，结果已显示在下方")
      }
    } catch (err) {
      if (waitingJobIdRef.current !== jobId) return
      const message = err instanceof Error ? err.message : String(err)
      setError(message)
      toast.error(message)
    } finally {
      if (waitingJobIdRef.current === jobId) {
        waitingJobIdRef.current = null
        setWaitingJobId(null)
      }
    }
  }

  const okRuns = runs.filter((r) => r.status === "ok")
  const failedRuns = runs.filter((r) => r.status === "error")

  const variantInputs = useMemo(() => {
    if (submittedVariants.length) return submittedVariants
    const ordered: string[] = []
    const seen = new Set<string>()
    for (const run of okRuns) {
      for (const row of run.results) {
        if (!seen.has(row.input)) {
          seen.add(row.input)
          ordered.push(row.input)
        }
      }
    }
    return ordered
  }, [submittedVariants, okRuns])

  const runMaps = useMemo(() => {
    const maps = new Map<ToolId, Map<string, VariantResult>>()
    for (const run of okRuns) {
      maps.set(run.tool, resultLookup(run.results))
    }
    return maps
  }, [okRuns])

  const focusedInput =
    variantInputs[Math.min(locusIndex, Math.max(variantInputs.length - 1, 0))] ?? null

  const headerBadge =
    selectedTools.length === 0
      ? genomeLabel(genome)
      : compareMode
        ? `${selectedTools.map(toolLabel).join(" + ")} · ${genomeLabel(genome)}`
        : `${toolLabel(selectedTools[0])} · ${genomeLabel(genome)}`

  return (
    <AppShell
      title="变异注释"
      description="选择注释工具后提交变异；多选工具时可对比。输入坐标所属基因组用 hg19/hg38 切换。"
      crumbs={[
        { label: "工具", href: "/tools/annotate/" },
        { label: "变异注释" },
      ]}
      actions={
        <>
          <Badge variant="outline">{headerBadge}</Badge>
          {engines.find((e) => e.id === "vep")?.version ? (
            <Badge variant="secondary">
              VEP {engines.find((e) => e.id === "vep")!.version}
            </Badge>
          ) : null}
        </>
      }
    >
      <div className="space-y-6">
        <Card className="overflow-hidden">
          <CardHeader>
            <CardTitle>输入与运行</CardTitle>
            <CardDescription>
              选择注释工具（可多选对比）；变异坐标所属基因组在输入区切换。任务在后台执行，可在「任务历史」查看记录。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Label>注释工具</Label>
                <span className="text-xs text-muted-foreground">
                  {compareMode ? "已多选，将对比同一基因组下的结果" : "可多选 VEP 与 ANNOVAR 进行对比"}
                </span>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                {TOOLS.map((tool) => {
                  const ready = isToolReady(engines, tool.id, genome)
                  const on = tools.has(tool.id)
                  const eng = engines.find((e) => e.id === tool.id)
                  const meta = toolMeta(engines, tool.id, genome)
                  return (
                    <button
                      key={tool.id}
                      type="button"
                      disabled={!ready || submitting}
                      aria-pressed={on}
                      onClick={() => toggleTool(tool.id)}
                      className={cn(
                        "flex items-start gap-3 rounded-lg border px-3 py-3 text-left transition-colors",
                        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        ready
                          ? on
                            ? "border-primary bg-primary/5"
                            : "hover:bg-muted/50"
                          : "cursor-not-allowed opacity-50",
                      )}
                    >
                      <span
                        className={cn(
                          "mt-0.5 inline-flex size-4 shrink-0 items-center justify-center rounded-sm border",
                          on && ready
                            ? "border-primary bg-primary text-primary-foreground"
                            : "border-muted-foreground/40",
                        )}
                      >
                        {on && ready ? <Check className="size-3" /> : null}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex flex-wrap items-center gap-2">
                          <span className="font-medium">{tool.label}</span>
                          {!ready ? (
                            <Badge variant="secondary">
                              {eng?.disabled_reason ? "未开放" : "未就绪"}
                            </Badge>
                          ) : on ? (
                            <Badge variant="outline">已选</Badge>
                          ) : null}
                        </span>
                        <span className="mt-1 block text-xs text-muted-foreground">
                          {ready
                            ? versionSummary(engines, tool.id, genome)
                            : eng?.disabled_reason
                              ? "当前服务未对该工具开放"
                              : `当前无 ${genomeLabel(genome)} 数据`}
                        </span>
                        {tool.id === "vep" && meta?.gencode ? (
                          <span className="mt-0.5 block text-xs text-muted-foreground">
                            {meta.gencode}
                            {meta.dbsnp ? ` · dbSNP ${meta.dbsnp}` : ""}
                          </span>
                        ) : null}
                      </span>
                    </button>
                  )
                })}
              </div>
            </div>

            <Separator />

            <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end">
              <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Label className="mb-0">变异输入</Label>
                    <div
                      role="group"
                      aria-label="参考基因组"
                      className="inline-flex rounded-md border bg-muted/40 p-0.5"
                    >
                      {GENOMES.map((g) => {
                        const on = genome === g.id
                        return (
                          <button
                            key={g.id}
                            type="button"
                            role="radio"
                            aria-checked={on}
                            disabled={submitting}
                            onClick={() => changeGenome(g.id)}
                            className={cn(
                              "rounded-[5px] px-2.5 py-1 text-xs font-medium transition-colors",
                              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                              on
                                ? "bg-background text-foreground shadow-sm"
                                : "text-muted-foreground hover:text-foreground",
                            )}
                          >
                            {g.alias}
                          </button>
                        )
                      })}
                    </div>
                    <span className="text-xs text-muted-foreground">
                      {genomeLabel(genome)}
                    </span>
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={loadSample}
                    disabled={submitting}
                  >
                    填入样例
                  </Button>
                </div>
                <VariantEditor
                  value={text}
                  onChange={setText}
                  language="vcf"
                  height={runs.length ? "180px" : "240px"}
                />
                <p className="text-xs text-muted-foreground">
                  坐标须与已选基因组一致。支持 <code>17:7577120 C&gt;T</code> 或含表头的
                  VCF；以 <code>#</code> 开头的行会被忽略。
                </p>
              </div>
              <div className="flex flex-col gap-2 pb-6">
                <Button
                  onClick={onSubmit}
                  disabled={
                    submitting || !selectedTools.length || cooldownLeft > 0
                  }
                >
                  {submitting ? <Loader2 className="size-4 animate-spin" /> : null}
                  {cooldownLeft > 0
                    ? `请稍候（${cooldownLeft}s）`
                    : compareMode
                      ? `运行并对比（${selectedTools.length} 种工具）`
                      : "运行注释"}
                </Button>
                {submitting ? (
                  <span className="text-xs text-muted-foreground">正在提交任务…</span>
                ) : cooldownLeft > 0 ? (
                  <span className="text-xs text-muted-foreground">
                    冷却中：{cooldownLeft}s 后可再次提交新任务
                  </span>
                ) : busyWaiting ? (
                  <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                    <Loader2 className="size-3 animate-spin" />
                    后台任务 {waitingJobId?.slice(0, 8)}… 运行中（可继续改输入，冷却结束后可再提交）
                  </span>
                ) : null}
              </div>
            </div>

            {error ? (
              <Alert variant="destructive">
                <AlertTitle>无法完成</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            ) : null}
          </CardContent>
        </Card>

        <Card ref={resultsRef}>
          <CardHeader className="pb-3">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="space-y-1.5">
                <CardTitle>
                  {okRuns.length > 1 ? "位点对比" : "注释结果"}
                </CardTitle>
                <CardDescription>
                  {runs.length === 0
                    ? "提交任务或从下方历史载入后，将在此展示 Gene / Transcript / c. / p.。"
                    : `基因组 ${genomeLabel(genome)}${
                        loadedJobId ? ` · 任务 ${loadedJobId.slice(0, 8)}` : ""
                      } · 点击位点切换查看`}
                </CardDescription>
              </div>
              <Button type="button" variant="outline" size="sm" asChild>
                <a href="/tools/jobs/">
                  <History className="size-4" />
                  全部历史
                </a>
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="rounded-lg border bg-muted/20 p-3">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <History className="size-4 text-muted-foreground" />
                  成功任务历史
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => void refreshHistory()}
                  disabled={loadingHistory}
                >
                  刷新
                </Button>
              </div>
              {history.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  暂无成功记录。完成一次注释后会出现在这里，点击即可回填结果。
                </p>
              ) : (
                <div className="flex flex-col gap-2">
                  {history.slice(0, 8).map((job) => {
                    const active = loadedJobId === job.id
                    return (
                      <button
                        key={job.id}
                        type="button"
                        disabled={loadingHistory || submitting}
                        onClick={() => void loadHistoryJob(job.id)}
                        className={cn(
                          "flex flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-2 text-left text-sm transition-colors",
                          active
                            ? "border-primary bg-primary/5"
                            : "hover:bg-background",
                        )}
                      >
                        <span className="min-w-0">
                          <span className="font-medium">
                            {job.assembly} · {(job.engines || []).join(" + ")}
                          </span>
                          <span className="mt-0.5 block text-xs text-muted-foreground">
                            {formatJobTime(job.created_at)} · {job.variant_count}{" "}
                            个位点
                            {job.variants_preview?.length
                              ? ` · ${job.variants_preview[0]}`
                              : ""}
                          </span>
                        </span>
                        <Badge variant={active ? "default" : "outline"}>
                          {active ? "当前" : "载入"}
                        </Badge>
                      </button>
                    )
                  })}
                </div>
              )}
            </div>

            {loadingHistory ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                正在载入历史结果…
              </div>
            ) : null}

            {runs.length === 0 ? (
              <div className="rounded-lg border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">
                尚无结果。提交新任务，或点选上方成功历史以查看注释明细。
              </div>
            ) : (
              <>
                {failedRuns.length > 0 ? (
                  <Alert variant="destructive">
                    <AlertTitle>部分工具失败</AlertTitle>
                    <AlertDescription>
                      {failedRuns.map((r) => (
                        <div key={r.tool}>
                          {toolLabel(r.tool)}：{r.error}
                        </div>
                      ))}
                    </AlertDescription>
                  </Alert>
                ) : null}

                {okRuns.length === 0 ? (
                  <p className="text-sm text-muted-foreground">没有成功的注释结果。</p>
                ) : (
                  <>
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm text-muted-foreground">位点</span>
                      {variantInputs.map((input, idx) => {
                        const geneHint =
                          okRuns
                            .map((run) => runMaps.get(run.tool)?.get(input)?.gene)
                            .find(Boolean) || null
                        return (
                          <Button
                            key={input}
                            type="button"
                            size="sm"
                            variant={idx === locusIndex ? "default" : "outline"}
                            onClick={() => setLocusIndex(idx)}
                            className="max-w-full font-mono text-xs"
                          >
                            {input}
                            {geneHint ? (
                              <span className="ml-1 opacity-80">· {geneHint}</span>
                            ) : null}
                          </Button>
                        )
                      })}
                      <span className="text-xs text-muted-foreground">
                        {variantInputs.length
                          ? `${Math.min(locusIndex + 1, variantInputs.length)} / ${variantInputs.length}`
                          : null}
                      </span>
                    </div>

                    {focusedInput ? (
                      <div className="rounded-xl border bg-card p-4 shadow-sm">
                        <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
                          <div>
                            <div className="font-mono text-base font-semibold tracking-tight">
                              {focusedInput}
                            </div>
                            <p className="mt-1 text-xs text-muted-foreground">
                              {genomeLabel(genome)}
                              {okRuns.length > 1
                                ? " · 对齐对比 Gene / Transcript / c. / p."
                                : " · 单工具注释"}
                            </p>
                          </div>
                          <div className="flex flex-wrap gap-1">
                            {okRuns.map((run) => {
                              const gene = runMaps.get(run.tool)?.get(focusedInput)?.gene
                              return (
                                <Badge key={run.tool} variant="secondary">
                                  {toolLabel(run.tool)}
                                  {gene ? ` · ${gene}` : ""}
                                </Badge>
                              )
                            })}
                          </div>
                        </div>

                        <div className="overflow-x-auto">
                          <table className="w-full min-w-[28rem] border-collapse text-sm">
                            <thead>
                              <tr className="border-b">
                                <th className="w-28 px-2 py-2 text-left font-medium text-muted-foreground">
                                  字段
                                </th>
                                {okRuns.map((run) => (
                                  <th
                                    key={run.tool}
                                    className="px-2 py-2 text-left font-medium"
                                  >
                                    <div>{toolLabel(run.tool)}</div>
                                    <div className="text-[11px] font-normal leading-snug text-muted-foreground">
                                      {versionSummary(engines, run.tool, run.genome)}
                                    </div>
                                  </th>
                                ))}
                              </tr>
                            </thead>
                            <tbody>
                              {COMPARE_FIELDS.map((field) => {
                                const values = okRuns.map(
                                  (run) =>
                                    runMaps.get(run.tool)?.get(focusedInput)?.[field.key],
                                )
                                const kind =
                                  okRuns.length > 1
                                    ? compareFieldValues(field.key, values)
                                    : "same"
                                const label = COMPARE_KIND_LABEL[kind]
                                return (
                                  <tr
                                    key={field.key}
                                    className={cn(
                                      "border-b last:border-0",
                                      compareKindClass(kind),
                                    )}
                                  >
                                    <td className="px-2 py-2.5 align-top font-medium text-muted-foreground">
                                      {field.label}
                                      {label ? (
                                        <span
                                          className={cn(
                                            "ml-1 text-[10px] font-normal",
                                            compareKindBadgeClass(kind),
                                          )}
                                        >
                                          {label}
                                        </span>
                                      ) : null}
                                    </td>
                                    {okRuns.map((run, i) => (
                                      <td
                                        key={run.tool}
                                        className="px-2 py-2.5 align-top font-mono text-[13px] leading-snug break-all"
                                      >
                                        <FieldPrimaryCell
                                          value={values[i]}
                                          row={runMaps.get(run.tool)?.get(focusedInput)}
                                          fieldKey={field.key}
                                        />
                                      </td>
                                    ))}
                                  </tr>
                                )
                              })}
                            </tbody>
                          </table>
                        </div>

                        <AllTranscriptsPanel
                          runs={okRuns}
                          input={focusedInput}
                          runMaps={runMaps}
                        />
                      </div>
                    ) : (
                      <p className="text-sm text-muted-foreground">无可展示位点。</p>
                    )}
                  </>
                )}
              </>
            )}
          </CardContent>
        </Card>
      </div>
    </AppShell>
  )
}
