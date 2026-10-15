import { useEffect, useMemo, useState } from "react"
import { FileSearch, Loader2, Search } from "lucide-react"

import {
  ExternalRef,
  GENE_KNOWLEDGE_SOURCES,
  KnowledgeSourcesCard,
  ManeVersionNote,
  accessionLookupUrl,
  geneLookupUrl,
} from "@/components/knowledge/KnowledgeSources"
import { AppShell } from "@/layouts/AppShell"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  fetchKnowledgeMeta,
  searchKnowledgeGenes,
  searchKnowledgeTranscripts,
  type KnowledgeGene,
  type KnowledgeMeta,
  type KnowledgeTranscript,
} from "@/lib/api"
import { CONSEQUENCE_CROSSWALK } from "@/lib/annotation-knowledge"
import { cn } from "@/lib/utils"

const DEFAULT_GENE = "TP53"

function initialGeneQuery() {
  return new URLSearchParams(window.location.search).get("q")?.trim() || DEFAULT_GENE
}

function AccessionLink({ id }: { id: string }) {
  const href = accessionLookupUrl(id)
  if (!href) return <span className="font-mono text-xs">{id}</span>
  return (
    <ExternalRef href={href} className="font-mono text-xs">
      {id}
    </ExternalRef>
  )
}

function pickDefaultGene(results: KnowledgeGene[], q: string): KnowledgeGene | null {
  if (!results.length) return null
  const needle = q.trim().toUpperCase()
  const exact = results.find((g) => g.symbol.toUpperCase() === needle)
  return exact || results[0]
}

export default function GeneKnowledgeApp() {
  const [meta, setMeta] = useState<KnowledgeMeta | null>(null)
  const [metaError, setMetaError] = useState<string | null>(null)

  const [geneQ, setGeneQ] = useState(initialGeneQuery)
  const [geneLoading, setGeneLoading] = useState(false)
  const [genes, setGenes] = useState<KnowledgeGene[]>([])
  const [geneTotal, setGeneTotal] = useState(0)
  const [geneError, setGeneError] = useState<string | null>(null)
  const [resolution, setResolution] = useState<string | null>(null)
  const [txError, setTxError] = useState<string | null>(null)
  const [selected, setSelected] = useState<KnowledgeGene | null>(null)

  const [txLoading, setTxLoading] = useState(false)
  const [transcripts, setTranscripts] = useState<KnowledgeTranscript[]>([])
  const [showCrosswalk, setShowCrosswalk] = useState(false)

  useEffect(() => {
    fetchKnowledgeMeta()
      .then(setMeta)
      .catch((e: Error) => setMetaError(e.message))
  }, [])

  useEffect(() => {
    let cancelled = false
    setGeneLoading(true); setGeneError(null); setResolution(null); setSelected(null); setGenes([]); setTranscripts([])
    const t = window.setTimeout(() => {
      searchKnowledgeGenes({ q: geneQ, limit: 30 })
        .then((data) => {
          if (cancelled) return
          setGenes(data.results)
          setGeneTotal(data.total)
          setResolution(data.query_resolution ? `按 MANE 主编号 ${data.query_resolution.accession} 定位基因；输入中的版本号未核验。` : null)
          setSelected((prev) => {
            if (prev && data.results.some((g) => g.gene_id === prev.gene_id)) {
              return prev
            }
            return pickDefaultGene(data.results, geneQ)
          })
        })
        .catch((error: Error) => {
          if (!cancelled) {
            setGeneError(error.message)
            setGenes([])
            setGeneTotal(0)
            setSelected(null)
          }
        })
        .finally(() => {
          if (!cancelled) setGeneLoading(false)
        })
    }, 220)
    return () => {
      cancelled = true
      window.clearTimeout(t)
    }
  }, [geneQ])

  useEffect(() => {
    if (!selected?.symbol) {
      setTranscripts([]); setTxLoading(false); setTxError(null)
      return
    }
    let cancelled = false
    setTxLoading(true); setTranscripts([]); setTxError(null)
    searchKnowledgeTranscripts({
      q: selected.symbol,
      maneOnly: true,
      limit: 40,
    })
      .then((data) => {
        if (cancelled) return
        // Prefer rows for this gene symbol
        const mine = data.results.filter(
          (r) => (r.gene || "").toUpperCase() === selected.symbol.toUpperCase(),
        )
        setTranscripts(mine)
      })
      .catch((error: Error) => {
        if (!cancelled) { setTranscripts([]); setTxError(error.message) }
      })
      .finally(() => {
        if (!cancelled) setTxLoading(false)
      })
  }, [selected?.symbol])

  const geneCountLabel = useMemo(() => {
    const n = meta?.gene_loaded ?? meta?.genes?.gene_count
    return typeof n === "number" ? n.toLocaleString() : "—"
  }, [meta])

  const ncbiGeneHref = selected
    ? `https://www.ncbi.nlm.nih.gov/gene/${selected.gene_id}`
    : null
  const geneHref = selected ? geneLookupUrl(selected.symbol) : null

  return (
    <AppShell
      title="基因"
      description="以基因为主条目，查看标准 ID、转录本和本地 MANE 映射，再核对关联证据。"
      crumbs={[
        { label: "知识库" },
        { label: "基因" },
      ]}
    >
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_17rem] lg:items-start">
        {/* Main: search + gene detail */}
        <div className="min-w-0 space-y-4">
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              className="h-11 pl-10 text-base"
              aria-label="搜索基因与转录本"
              placeholder="搜索基因符号 / 别名 / GeneID / NM_ / ENST…"
              value={geneQ}
              onChange={(e) => setGeneQ(e.target.value)}
              autoFocus
            />
          </div>

          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>默认示例：{DEFAULT_GENE}</span>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-7 px-2"
              onClick={() => setGeneQ(DEFAULT_GENE)}
            >
              重置为 {DEFAULT_GENE}
            </Button>
            <span className="text-border">·</span>
            {geneLoading ? (
              <span className="inline-flex items-center gap-1">
                <Loader2 className="size-3 animate-spin" /> 搜索中…
              </span>
            ) : (
              <span>
                匹配 {geneTotal.toLocaleString()} 条
                {genes.length ? `，列表 ${genes.length} 条` : ""}
              </span>
            )}
          </div>

          {resolution && <p className="text-sm/relaxed text-muted-foreground" role="status">{resolution}</p>}
          {geneError && <p className="text-sm text-destructive" role="alert">基因查询失败：{geneError}</p>}
          <div className="grid gap-4 md:grid-cols-[13rem_minmax(0,1fr)]">
            {/* Hit list */}
            <div className="max-h-[28rem] overflow-y-auto rounded-xl border">
              {genes.length === 0 ? (
                <p className="p-3 text-sm text-muted-foreground">
                  {geneLoading ? "正在查询…" : geneError ? "无法读取，请重试查询" : meta?.ready === false ? "请先导入知识库" : "无匹配基因"}
                </p>
              ) : (
                <ul className="divide-y">
                  {genes.map((g) => {
                    const active = selected?.gene_id === g.gene_id
                    return (
                      <li key={g.gene_id}>
                        <button
                          type="button"
                          onClick={() => setSelected(g)}
                          className={cn(
                            "flex w-full flex-col items-start gap-0.5 px-3 py-2.5 text-left transition-colors hover:bg-muted/50",
                            active && "bg-muted",
                          )}
                        >
                          <span className="font-medium">{g.symbol}</span>
                          <span className="line-clamp-1 text-[11px] text-muted-foreground">
                            {g.chromosome ? `chr${g.chromosome}` : "—"} ·{" "}
                            {g.gene_id}
                          </span>
                        </button>
                      </li>
                    )
                  })}
                </ul>
              )}
            </div>

            {/* Selected gene panel */}
            <Card className="min-w-0">
              {!selected ? (
                <CardContent className="py-10 text-sm text-muted-foreground">
                  从左侧选择基因，或输入符号开始搜索。
                </CardContent>
              ) : (
                <>
                  <CardHeader className="pb-3">
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div>
                        <CardTitle className="text-2xl tracking-tight">
                          {geneHref ? (
                            <ExternalRef href={geneHref} className="no-underline hover:underline">
                              {selected.symbol}
                            </ExternalRef>
                          ) : (
                            selected.symbol
                          )}
                        </CardTitle>
                        <CardDescription className="mt-1 max-w-xl text-sm">
                          {selected.name || "—"}
                        </CardDescription>
                      </div>
                      <Badge variant="secondary">{selected.type}</Badge>
                    </div>
                  </CardHeader>
                  <CardContent className="flex flex-col gap-5">
                    <Button asChild variant="outline" className="w-fit"><a href={`/evidence/clinical/?q=${encodeURIComponent(selected.symbol)}`}><FileSearch data-icon="inline-start" />查看关联证据</a></Button>
                    <dl className="grid gap-3 sm:grid-cols-2">
                      <div className="rounded-lg border px-3 py-2">
                        <dt className="text-xs text-muted-foreground">NCBI GeneID</dt>
                        <dd className="mt-0.5 font-mono text-sm">
                          {ncbiGeneHref ? (
                            <ExternalRef href={ncbiGeneHref}>
                              {selected.gene_id}
                            </ExternalRef>
                          ) : (
                            selected.gene_id
                          )}
                        </dd>
                      </div>
                      <div className="rounded-lg border px-3 py-2">
                        <dt className="text-xs text-muted-foreground">染色体 / 定位</dt>
                        <dd className="mt-0.5 text-sm">
                          {selected.chromosome || "—"}
                          {selected.map_location
                            ? ` · ${selected.map_location}`
                            : ""}
                        </dd>
                      </div>
                      <div className="rounded-lg border px-3 py-2">
                        <dt className="text-xs text-muted-foreground">Ensembl Gene</dt>
                        <dd className="mt-0.5 font-mono text-sm">
                          {selected.ensembl_gene ? (
                            <AccessionLink
                              id={selected.ensembl_gene.split(".")[0]}
                            />
                          ) : (
                            "—"
                          )}
                        </dd>
                      </div>
                      <div className="rounded-lg border px-3 py-2">
                        <dt className="text-xs text-muted-foreground">HGNC</dt>
                        <dd className="mt-0.5 font-mono text-sm">
                          {selected.hgnc_id ? (
                            <ExternalRef
                              href={`https://www.genenames.org/data/gene-symbol-report/#!/hgnc_id/HGNC:${selected.hgnc_id}`}
                            >
                              HGNC:{selected.hgnc_id}
                            </ExternalRef>
                          ) : (
                            "—"
                          )}
                        </dd>
                      </div>
                    </dl>

                    {selected.synonyms && selected.synonyms.length > 0 ? (
                      <div>
                        <div className="mb-1.5 text-xs font-medium text-muted-foreground">
                          别名
                        </div>
                        <div className="flex flex-wrap gap-1">
                          {selected.synonyms.slice(0, 24).map((s) => (
                            <Badge key={s} variant="outline" className="font-normal">
                              {s}
                            </Badge>
                          ))}
                          {selected.synonyms.length > 24 ? (
                            <span className="text-xs text-muted-foreground">
                              +{selected.synonyms.length - 24}
                            </span>
                          ) : null}
                        </div>
                      </div>
                    ) : null}

                    <div>
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <div className="text-sm font-medium">
                          MANE 转录本（ENST ↔ NM）
                        </div>
                        {txLoading ? (
                          <Loader2 className="size-3.5 animate-spin text-muted-foreground" />
                        ) : (
                          <span className="text-xs text-muted-foreground">
                            {transcripts.length} 条 · MANE v
                            {meta?.mane?.version || "—"}
                          </span>
                        )}
                      </div>
                      <div className="overflow-x-auto rounded-lg border">
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>Accession</TableHead>
                              <TableHead>对应 ID</TableHead>
                              <TableHead>类型</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {transcripts.length === 0 ? (
                              <TableRow>
                                <TableCell
                                  colSpan={3}
                                  className="text-muted-foreground"
                                >
                                  {txLoading
                                    ? "加载中…"
                                    : txError ? `映射读取失败：${txError}` : "该基因暂无 MANE 映射（或未导入）"}
                                </TableCell>
                              </TableRow>
                            ) : (
                              transcripts.map((row) => {
                                const linked = [
                                  ...(row.ensembl || []),
                                  ...(row.refseq || []),
                                ]
                                return (
                                  <TableRow key={row.accession}>
                                    <TableCell>
                                      <AccessionLink id={row.accession} />
                                    </TableCell>
                                    <TableCell>
                                      {linked.length ? (
                                        <span className="flex flex-wrap gap-x-2 gap-y-1">
                                          {linked.map((id) => (
                                            <AccessionLink key={id} id={id} />
                                          ))}
                                        </span>
                                      ) : (
                                        "—"
                                      )}
                                    </TableCell>
                                    <TableCell>
                                      {row.mane ? (
                                        <Badge>
                                          {row.mane_status === "plus_clinical"
                                            ? "Plus Clinical"
                                            : "Select"}
                                        </Badge>
                                      ) : (
                                        "—"
                                      )}
                                    </TableCell>
                                  </TableRow>
                                )
                              })
                            )}
                          </TableBody>
                        </Table>
                      </div>
                    </div>
                  </CardContent>
                </>
              )}
            </Card>
          </div>

          <div className="pt-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="text-muted-foreground"
              onClick={() => setShowCrosswalk((v) => !v)}
            >
              {showCrosswalk ? "收起" : "展开"}效应术语对照（VEP ↔ ANNOVAR）
            </Button>
            {showCrosswalk ? (
              <Card className="mt-2">
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">效应术语对照</CardTitle>
                  <CardDescription>
                    注释对比时若落在同一行，标为「表述不同」。
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="overflow-x-auto rounded-lg border">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>含义</TableHead>
                          <TableHead>VEP</TableHead>
                          <TableHead className="w-8 text-center">≈</TableHead>
                          <TableHead>ANNOVAR</TableHead>
                          <TableHead>常见拼法</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {CONSEQUENCE_CROSSWALK.map((row) => (
                          <TableRow key={row.id}>
                            <TableCell className="align-top">
                              <div className="font-medium">{row.label}</div>
                              <div className="text-xs text-muted-foreground">
                                {row.meaning}
                              </div>
                            </TableCell>
                            <TableCell className="align-top">
                              <div className="flex flex-col gap-1">
                                {row.vep.map((term) => (
                                  <code
                                    key={term}
                                    className="w-fit rounded bg-sky-50 px-1.5 py-0.5 font-mono text-[11px] dark:bg-sky-950/40"
                                  >
                                    {term}
                                  </code>
                                ))}
                              </div>
                            </TableCell>
                            <TableCell className="align-middle text-center text-muted-foreground">
                              ≈
                            </TableCell>
                            <TableCell className="align-top">
                              <div className="flex flex-col gap-1">
                                {row.annovar
                                  .filter((t) => !t.includes(","))
                                  .map((term) => (
                                    <code
                                      key={term}
                                      className="w-fit rounded bg-amber-50 px-1.5 py-0.5 font-mono text-[11px] dark:bg-amber-950/40"
                                    >
                                      {term}
                                    </code>
                                  ))}
                              </div>
                            </TableCell>
                            <TableCell className="align-top font-mono text-[11px] text-muted-foreground">
                              {row.annovarExample || "—"}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </CardContent>
              </Card>
            ) : null}
          </div>
        </div>

        {/* Side rail: sources + MANE note */}
        <aside className="space-y-3 lg:sticky lg:top-20">
          <KnowledgeSourcesCard
            compact
            sources={GENE_KNOWLEDGE_SOURCES}
            description={
              meta?.ready
                ? `已导入基因 ${geneCountLabel} 条` +
                  (meta.transcript_loaded
                    ? ` · 映射 ${meta.transcript_loaded.toLocaleString()}`
                    : "")
                : metaError ||
                  meta?.error ||
                  "未导入，请运行 import_gene_knowledge.py"
            }
          />
          <ManeVersionNote compact mane={meta?.mane} />
        </aside>
      </div>
    </AppShell>
  )
}
