import { useEffect, useState } from "react"
import { Loader2 } from "lucide-react"

import { ExternalRef } from "@/components/knowledge/KnowledgeSources"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import {
  searchTranscriptStructures,
  type TranscriptStructure,
  type TranscriptStructureResults,
  type TranscriptStructureSource,
} from "@/lib/api"

const PAGE_SIZE = 20

function SourceDetails({ source }: { source: TranscriptStructureSource | null }) {
  if (!source) return <p className="text-muted-foreground">尚无核验来源记录。</p>
  return (
    <dl className="space-y-1 break-all text-xs">
      <div><dt className="inline text-muted-foreground">来源：</dt><dd className="inline">{source.name || "未记录名称"}</dd></div>
      <div>
        <dt className="inline text-muted-foreground">原始记录：</dt>
        <dd className="inline">
          {source.url && /^https?:\/\//i.test(source.url)
            ? <ExternalRef href={source.url}>{source.url}</ExternalRef>
            : "未记录链接"}
        </dd>
      </div>
      <div><dt className="inline text-muted-foreground">来源版本：</dt><dd className="inline">{source.release || "未标明"}</dd></div>
      <div><dt className="inline text-muted-foreground">获取时间：</dt><dd className="inline">{source.retrieved_at || "未记录"}</dd></div>
      <div><dt className="inline text-muted-foreground">SHA-256：</dt><dd className="inline font-mono">{source.sha256 || "未记录"}</dd></div>
    </dl>
  )
}

function mappingCounts(row: TranscriptStructure, assembly: string) {
  const counts = [...new Set(row.mappings.filter((mapping) => mapping.assembly === assembly).map((mapping) => mapping.raw_exon_count))]
  return counts.length ? counts.sort((a, b) => a - b).join(" / ") : "无映射"
}

function StructureDetails({ row }: { row: TranscriptStructure }) {
  return (
    <div className="space-y-4 rounded-lg border bg-muted/20 p-4 text-sm" role="region" aria-label={`${row.accession} 结构详情`}>
      <div>
        <h3 className="font-medium">{row.accession} · {row.gene}</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          {row.mane_status ? `MANE：${row.mane_status} · ` : ""}
          Ensembl：{row.ensembl_accessions.length ? row.ensembl_accessions.join("、") : "无对应记录"}
        </p>
      </div>
      <section className="space-y-2">
        <h4 className="font-medium">转录本外显子与核验来源</h4>
        <p className="text-xs text-muted-foreground">以下为转录本序列坐标，1-based 闭区间；外显子总数包含 UTR 外显子。</p>
        <SourceDetails source={row.exon_source} />
        {row.exon_support ? (
          <div className="space-y-2 rounded border bg-background p-3">
            <p className="text-xs">
              补充核验依据：官方声明的替代版本 <span className="font-mono">{row.exon_support.accession}</span>。
              脚本核对了外显子序列及边界；当前结果仍属于 <span className="font-mono">{row.accession}</span>。
            </p>
            <SourceDetails source={row.exon_support.source} />
          </div>
        ) : null}
        {row.verification_note ? (
          <p className="break-all text-xs text-muted-foreground">核验记录：{row.verification_note}</p>
        ) : null}
        {row.transcript_exons.length ? (
          <div className="flex max-h-48 flex-wrap gap-2 overflow-y-auto">
            {row.transcript_exons.map((exon, index) => (
              <span key={`${exon.start}:${exon.end}:${index}`} className="rounded border bg-background px-2 py-1 font-mono text-xs">
                {index + 1}: {exon.start}–{exon.end}
              </span>
            ))}
          </div>
        ) : <p className="text-xs text-muted-foreground">尚无已核验的转录本外显子区间。</p>}
        {row.exon_status !== "verified" && row.candidate_exon_count != null ? (
          <p className="text-xs text-muted-foreground">参考映射的候选计数为 {row.candidate_exon_count}，尚未核验，不作为正式外显子总数。</p>
        ) : null}
      </section>
      <section className="space-y-3">
        <h4 className="font-medium">基因组映射（全部位点）</h4>
        <p className="text-xs text-muted-foreground">基因组区间采用 0-based 半开区间 [start, end)。块数保留原始注释，不作为真实外显子总数。</p>
        {row.mappings.length ? row.mappings.map((mapping, index) => (
          <div key={`${mapping.assembly}:${mapping.chrom}:${mapping.start}:${mapping.end}:${index}`} className="space-y-2 rounded-md border bg-background p-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline">{mapping.assembly}</Badge>
              <span className="font-mono text-xs">{mapping.chrom}:{mapping.start}–{mapping.end} ({mapping.strand})</span>
              <span className="text-xs">原始映射块数：{mapping.raw_exon_count}</span>
            </div>
            <SourceDetails source={mapping.source} />
            <div className="max-h-32 overflow-y-auto break-words font-mono text-xs text-muted-foreground">
              {mapping.exon_starts.map((start, exonIndex) => `[${start}, ${mapping.exon_ends[exonIndex] ?? "?"})`).join(" · ")}
            </div>
          </div>
        )) : <p className="text-xs text-muted-foreground">暂无基因组映射。</p>}
      </section>
    </div>
  )
}

export function TranscriptStructures({ query }: { query: string }) {
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<TranscriptStructureResults | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [detailAccession, setDetailAccession] = useState<string | null>(null)
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setData(null)
    setError(null)
    setDetailAccession(null)
    if (!query) {
      setLoading(false)
      return () => controller.abort()
    }
    setLoading(true)
    const timer = window.setTimeout(() => {
      searchTranscriptStructures({ q: query, limit: PAGE_SIZE, offset, signal: controller.signal })
        .then((result) => {
          if (!controller.signal.aborted) setData(result)
        })
        .catch((reason: Error) => {
          if (!controller.signal.aborted) setError(reason.message)
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false)
        })
    }, 220)
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [query, offset, retry])

  const selectedRow = data?.results.find((row) => row.accession === detailAccession)
  const versionUnspecified = /^(?:(?:NM|NR)_?\d+|ENST\d+)$/i.test(query)
  const exactVersion = /^(?:(?:NM|NR)_?\d+|ENST\d+)\.\d+$/i.test(query)
  const rows = data?.results || []

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">转录本结构 / 外显子总数</CardTitle>
          {loading ? <Loader2 className="size-4 animate-spin text-muted-foreground" aria-label="正在加载转录本结构" /> : null}
        </div>
        <CardDescription>
          一个完整版本的转录本保存一个核验总数；GRCh37、GRCh38 分别保留基因组映射。
          映射块数可能受比对拆分影响，不等同于真实外显子总数。
          多个位点出现不同块数时，会并列显示全部不同值。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {!query ? <p className="text-sm text-muted-foreground">选择基因或输入完整的转录本编号，查看结构与核验来源。</p> : null}
        {error ? (
          <div className="flex flex-wrap items-center gap-2 text-sm" role="alert">
            <span className="text-destructive">结构读取失败：{error}</span>
            <Button type="button" variant="outline" size="sm" onClick={() => setRetry((value) => value + 1)}>重试</Button>
          </div>
        ) : null}
        {data?.ready === false ? <p className="text-sm text-muted-foreground">转录本结构参考库尚未配置或导入，暂无可用结果。</p> : null}
        {versionUnspecified && data?.ready ? <p className="text-xs text-muted-foreground">输入未指定版本，以下列出参考库中的具体版本；不代表输入版本已经核验。</p> : null}
        {data?.ready && rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {exactVersion ? `未找到 ${query} 的精确版本结构；不会使用其他版本代替。` : `暂无 ${query} 的结构记录。`}
          </p>
        ) : null}
        {rows.length ? (
          <>
            <div className="overflow-x-auto rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>完整转录本版本</TableHead>
                    <TableHead>外显子总数</TableHead>
                    <TableHead>核验状态</TableHead>
                    <TableHead>GRCh37 映射块数</TableHead>
                    <TableHead>GRCh38 映射块数</TableHead>
                    <TableHead className="text-right">操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((row) => {
                    const verified = row.exon_status === "verified" && Number.isInteger(row.exon_count) && (row.exon_count ?? 0) > 0
                    return (
                      <TableRow key={row.accession}>
                        <TableCell className="font-mono text-xs">{row.accession}</TableCell>
                        <TableCell>{verified ? row.exon_count : "待核验"}</TableCell>
                        <TableCell><Badge variant={verified ? "secondary" : "outline"}>{verified ? "来源已核验" : "未核验"}</Badge></TableCell>
                        <TableCell>{mappingCounts(row, "GRCh37")}</TableCell>
                        <TableCell>{mappingCounts(row, "GRCh38")}</TableCell>
                        <TableCell className="text-right">
                          <Button type="button" size="sm" variant="ghost" aria-label={`查看 ${row.accession} 详情`} aria-expanded={detailAccession === row.accession} onClick={() => setDetailAccession((current) => current === row.accession ? null : row.accession)}>
                            {detailAccession === row.accession ? "收起" : "详情"}
                          </Button>
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
              <span>共 {data!.total.toLocaleString()} 条，当前 {offset + 1}–{offset + rows.length} 条{data!.release ? ` · 结构库版本 ${data!.release}` : ""}</span>
              <div className="flex gap-2">
                <Button type="button" variant="outline" size="sm" disabled={loading || offset === 0} onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}>上一页</Button>
                <Button type="button" variant="outline" size="sm" disabled={loading || offset + rows.length >= data!.total} onClick={() => setOffset((value) => value + PAGE_SIZE)}>下一页</Button>
              </div>
            </div>
          </>
        ) : null}
        {selectedRow ? <StructureDetails row={selectedRow} /> : null}
      </CardContent>
    </Card>
  )
}
