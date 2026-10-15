import { useEffect, useState } from "react"
import { ArrowUpFromLine, Database, Dna, RefreshCw, ShieldCheck } from "lucide-react"

import { AppShell } from "@/layouts/AppShell"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { fetchKnowledgeUpdates, startKnowledgeUpdate, type KnowledgeUpdateStatus } from "@/lib/api"

function date(value?: string | null) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "暂无记录"
}

const statusNames = {
  idle: "尚未检查", running: "处理中", succeeded: "检查完成", failed: "未完成", interrupted: "已中断",
}

export default function DataUpdatesApp() {
  const [data, setData] = useState<KnowledgeUpdateStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const running = data?.status === "running"
  const busy = submitting || running

  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function load() {
      try {
        const next = await fetchKnowledgeUpdates(controller.signal)
        if (controller.signal.aborted) return
        setData(next)
        setError(null)
        if (next.status === "running") timer = setTimeout(load, 2000)
      } catch (err) {
        if (controller.signal.aborted) return
        setError(err instanceof Error ? err.message : "读取更新状态失败")
      }
    }
    void load()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [refresh])

  async function start(action: "check" | "update") {
    setSubmitting(true)
    setError(null)
    try {
      await startKnowledgeUpdate(action)
      setData((previous) => previous ? { ...previous, status: "running", message: "准备连接 NCBI 官方数据源", sources: undefined } : previous)
      setRefresh((value) => value + 1)
    } catch (err) {
      setError(err instanceof Error ? err.message : "提交失败")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <AppShell title="数据更新" description="追踪官方来源，让本地知识库保持可验证、可追溯。" crumbs={[{ label: "系统" }, { label: "数据更新" }]}>
      <div className="flex flex-col gap-6">
        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <CardTitle>本地知识库状态</CardTitle>
              <Badge variant="secondary">手动触发 · 未配置定时任务</Badge>
            </div>
            <CardDescription>检查会下载并校验官方文件，通常需要数分钟。内容相同则跳过更新，不会刷新本地更新时间。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-5">
            <dl className="grid gap-4 sm:grid-cols-3">
              <div><dt className="text-muted-foreground">本地最近成功更新</dt><dd className="mt-1 font-medium tabular-nums">{date(data?.current.imported_at)}</dd></div>
              <div><dt className="text-muted-foreground">最近成功检查</dt><dd className="mt-1 font-medium tabular-nums">{date(data?.last_checked_at)}</dd></div>
              <div><dt className="text-muted-foreground">当前快照</dt><dd className="mt-1 font-mono">{data ? data.current.release?.slice(0, 12) || "初始导入" : "—"}</dd></div>
            </dl>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" disabled={busy || !data} onClick={() => void start("check")}><RefreshCw data-icon="inline-start" className={busy ? "motion-safe:animate-spin" : undefined} />检查更新</Button>
              <Button disabled={busy || !data} onClick={() => void start("update")}><ArrowUpFromLine data-icon="inline-start" />检查并更新</Button>
              {error && <Button variant="ghost" onClick={() => setRefresh((value) => value + 1)}>重新读取状态</Button>}
            </div>
            <div role="status" aria-live="polite" className="flex flex-wrap items-center gap-2 text-muted-foreground">
              {data && <Badge variant={data.status === "failed" ? "destructive" : "outline"}>{data.update_available ? "发现更新" : statusNames[data.status]}</Badge>}
              <span>{data?.message || "正在读取更新状态…"}</span>
            </div>
          </CardContent>
        </Card>

        {error && <Alert variant="destructive"><AlertTitle>请求未完成</AlertTitle><AlertDescription>{error}。若服务启用了 API Key，请先在<a href="/tools/engines/">引擎与设置</a>中填写。</AlertDescription></Alert>}

        <div className="grid items-start gap-4 lg:grid-cols-2">
          {!data && !error ? <><Skeleton className="h-72" /><Skeleton className="h-72" /></> : data && (["gene_info", "mane"] as const).map((key) => {
            const gene = key === "gene_info"
            const local = data.current.updates?.sources[key]
            const checked = data.sources?.[key]
            const source = checked || local
            const Icon = gene ? Database : Dna
            const count = gene ? data.current.genes?.gene_count : data.current.mane?.mane_select
            return (
              <Card key={key}>
                <CardHeader>
                  <div className="flex items-center justify-between gap-2">
                    <CardTitle className="flex items-center gap-2"><Icon className="size-4" />{gene ? "NCBI Gene" : "MANE 转录本"}</CardTitle>
                    <Badge variant="outline">{checked ? checked.changed && !data.published ? "有变化" : "已核对" : "待检查"}</Badge>
                  </div>
                  <CardDescription>{gene ? "基因符号、别名、GeneID 与基础信息" : "RefSeq / Ensembl 转录本及蛋白标识符映射"}</CardDescription>
                </CardHeader>
                <CardContent className="flex flex-col gap-4">
                  <div><span className="text-2xl font-semibold tabular-nums">{count?.toLocaleString() ?? "—"}</span><span className="ml-2 text-muted-foreground">{gene ? "条 Gene 记录" : "条 MANE Select"}</span></div>
                  <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2">
                    <dt className="text-muted-foreground">当前版本</dt><dd>{gene ? "滚动更新 · Homo sapiens" : `v${data.current.mane?.version || "—"} · GRCh38`}</dd>
                    {!gene && <><dt className="text-muted-foreground">上游版本</dt><dd>{checked?.version ? `v${checked.version}` : "尚未检查"}</dd></>}
                    <dt className="text-muted-foreground">本地更新</dt><dd className="tabular-nums">{date(local?.last_updated_at || data.current.imported_at)}</dd>
                    <dt className="text-muted-foreground">上游文件时间</dt><dd className="tabular-nums">{date(source?.last_modified)}</dd>
                  </dl>
                  <p className="text-muted-foreground">{gene ? "包含蛋白编码、非编码和生物学区域等类型；沿用官方文件范围，含少量古人类记录，不等于蛋白编码基因数。" : `另有 ${data.current.mane?.mane_plus_clinical?.toLocaleString() ?? "—"} 条 Plus Clinical；不包含完整 RNA / 蛋白序列。`}</p>
                  <div className="flex flex-col gap-1 border-t pt-3">
                    <span className="text-muted-foreground">官方来源</span>
                    <a href={source?.url || data.sources_config[key]} target="_blank" rel="noreferrer" className="break-all underline underline-offset-4">{source?.url || data.sources_config[key]}</a>
                  </div>
                  {source?.content_sha256 && <details><summary className="cursor-pointer text-muted-foreground">查看内容指纹（SHA-256）</summary><p className="mt-2 break-all font-mono">{source.content_sha256}</p></details>}
                </CardContent>
              </Card>
            )
          })}
        </div>

        <Alert>
          <ShieldCheck />
          <AlertTitle>更新边界与保护</AlertTitle>
          <AlertDescription>Gene 使用固定文件名，MANE 自动识别官方版本；以解压后内容指纹判断变化。校验通过后一次性切换快照，旧快照保留，进行中的注释固定使用原版本。本页只更新 Gene / MANE，不会更改 VEP 或 ANNOVAR 的注释数据库。</AlertDescription>
        </Alert>
      </div>
    </AppShell>
  )
}
