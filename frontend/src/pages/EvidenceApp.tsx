import { useEffect, useState } from "react"
import { RefreshCw } from "lucide-react"
import { AppShell } from "@/layouts/AppShell"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Skeleton } from "@/components/ui/skeleton"
import { CaseSearch } from "@/components/evidence/CaseSearch"
import { DemoNotice, EvidenceEmpty, EvidenceRows, KindSelect, SourceSheet } from "@/components/evidence/EvidenceList"
import { IssueForm } from "@/components/evidence/IssueForm"
import { filterKind, fetchDemoCases, fetchEvidenceCatalog, type DemoCase, type Evidence } from "@/lib/evidence"
import { searchKnowledgeGenes } from "@/lib/api"

export default function EvidenceApp() {
  const [params] = useState(() => new URLSearchParams(window.location.search))
  const gene = params.get("gene")?.trim() ?? ""
  const [query, setQuery] = useState(gene || params.get("q") || "")
  const [aliases, setAliases] = useState<string[]>([])
  const [aliasStatus, setAliasStatus] = useState("")
  const [kind, setKind] = useState("all")
  const [catalog, setCatalog] = useState<Evidence[] | null>(null)
  const [examples, setExamples] = useState<DemoCase[]>([])
  const [error, setError] = useState("")
  const [attempt, setAttempt] = useState(0)
  const [selected, setSelected] = useState<Evidence | null>(null)
  const [issueOpen, setIssueOpen] = useState(false)
  const [runId, setRunId] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController(); setError("")
    Promise.allSettled([fetchEvidenceCatalog(controller.signal), fetchDemoCases(controller.signal)]).then(([items, cases]) => {
      if (controller.signal.aborted) return
      if (items.status === "fulfilled") setCatalog(items.value.items)
      else setError(items.reason.message)
      if (cases.status === "fulfilled") setExamples(cases.value.items)
      else setError(prev => prev || "虚构示例暂时无法加载，可以手动填写条件。")
    })
    return () => controller.abort()
  }, [attempt])
  useEffect(() => {
    if (!gene) return
    let active = true; setAliasStatus("正在核对本地基因符号与别名…")
    searchKnowledgeGenes({ q: gene, limit: 30 }).then(data => {
      if (!active) return
      const exact = data.results.find(item => item.symbol.toUpperCase() === gene.toUpperCase())
      setAliases(exact ? [exact.symbol, ...(exact.synonyms ?? [])] : [gene])
      setAliasStatus(exact ? "按本地基因符号与别名检索资料中的分子特征文字，不表示临床适用。" : "未找到标准基因符号，仅按输入文字筛选。")
    }).catch(() => { if (active) { setAliases([gene]); setAliasStatus("基因别名暂时无法读取，仅按输入符号筛选。") } })
    return () => { active = false }
  }, [gene])
  const linked = !!gene && query.trim().toUpperCase() === gene.toUpperCase()
  const visible = filterKind(catalog ?? [], kind).filter(item => {
    if (linked) {
      const mentions = (item.biomarker.match(/[A-Za-z][A-Za-z0-9-]*/g) ?? []).map(value => value.toUpperCase())
      return (aliases.length ? aliases : [gene]).some(alias => mentions.includes(alias.toUpperCase()))
    }
    const haystack = [item.title, item.summary, item.cancer, item.biomarker, item.drug_name, item.source_title, item.source_text].join(" ").toLowerCase()
    return haystack.includes(query.trim().toLowerCase())
  })
  return <AppShell title="教学流程验证" description="按关键词浏览资料，或用已核对的条件查询并查看出处。" crumbs={[{ label: "验证工具" }, { label: "教学流程" }]}>
    <DemoNotice />
    {error && <Alert variant="destructive"><AlertDescription><span>{error}</span><Button variant="outline" className="ml-2" onClick={() => setAttempt(value => value + 1)}><RefreshCw data-icon="inline-start" />重试</Button></AlertDescription></Alert>}
    <Tabs defaultValue={params.get("view") === "conditions" ? "conditions" : "catalog"} className="gap-5">
      <TabsList variant="line" className="border-b"><TabsTrigger value="catalog">资料目录</TabsTrigger><TabsTrigger value="conditions">条件检索</TabsTrigger></TabsList>
      <TabsContent value="catalog" className="flex flex-col gap-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end"><Field className="flex-1"><FieldLabel htmlFor="evidence-query">资料关键词</FieldLabel><Input id="evidence-query" value={query} onChange={e => setQuery(e.target.value)} placeholder="例如 EGFR、HER2、肺腺癌、药物名称" /></Field><KindSelect value={kind} onChange={setKind} /></div>
        {linked && <div className="flex flex-wrap items-center justify-between gap-2 text-sm"><p>{gene} · {aliasStatus}</p><Button asChild variant="outline"><a href={`/knowledge/genes/?q=${encodeURIComponent(gene)}`}>返回基因与转录本</a></Button></div>}
        {catalog === null ? (error ? <EvidenceEmpty title="资料暂时无法读取" description="请重试；不会用模拟结果替代加载失败。" /> : <div className="flex flex-col gap-4" role="status" aria-label="正在读取资料"><Skeleton className="h-28 w-full" /><Skeleton className="h-28 w-full" /></div>) : <>
          <p className="text-sm text-muted-foreground" role="status">显示 {visible.length} / {catalog.length} 条教学资料 · 目录浏览不作条件匹配。</p>
          {visible.length ? <EvidenceRows items={visible} onSelect={setSelected} /> : <EvidenceEmpty title="没有相关教学资料" description="当前目录覆盖有限，可清空关键词查看全部，或记录资料覆盖问题。" />}
          <Button variant="outline" className="w-fit" onClick={() => { setRunId(null); setIssueOpen(true) }}>记录资料问题</Button>
        </>}
      </TabsContent>
      <TabsContent value="conditions" forceMount className="data-[state=inactive]:hidden"><CaseSearch examples={examples} onSelect={setSelected} onIssue={run => { setRunId(run); setIssueOpen(true) }} onInvalidate={() => { setSelected(null); setRunId(null) }} /></TabsContent>
    </Tabs>
    <SourceSheet item={selected} onClose={() => setSelected(null)} />
    <IssueForm open={issueOpen} runId={runId} onClose={() => setIssueOpen(false)} onSaved={() => setIssueOpen(false)} />
  </AppShell>
}
