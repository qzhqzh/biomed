import { useEffect, useState } from "react"
import { Download, Plus, RefreshCw } from "lucide-react"
import { AppShell } from "@/layouts/AppShell"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { EvidenceEmpty } from "@/components/evidence/EvidenceList"
import { IssueForm } from "@/components/evidence/IssueForm"
import { CATEGORY_LABELS, downloadIssues, fetchIssues, formatDate, type Issue } from "@/lib/evidence"

export default function IssuesApp() {
  const [items, setItems] = useState<Issue[] | null>(null)
  const [filter, setFilter] = useState("all")
  const [error, setError] = useState("")
  const [attempt, setAttempt] = useState(0)
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<Issue | null>(null)
  useEffect(() => {
    const controller = new AbortController(); setError("")
    fetchIssues(controller.signal).then(data => { if (!controller.signal.aborted) setItems(data.items) }).catch(e => { if (!controller.signal.aborted) setError(e.message) })
    return () => controller.abort()
  }, [attempt])
  async function exportAll() {
    setBusy(true); setError("")
    try { await downloadIssues() } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  const visible = (items ?? []).filter(item => filter === "all" || item.status === filter)
  return <AppShell title="问题与经验" description="记录实际问题、核对预期表现，积累资料与生信流程的处理经验。" crumbs={[{ label: "知识库" }, { label: "问题与经验" }]}>
    <div className="flex flex-wrap items-center justify-between gap-3"><Select value={filter} onValueChange={setFilter}><SelectTrigger className="w-36" aria-label="问题状态"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="all">全部状态</SelectItem><SelectItem value="open">待处理</SelectItem><SelectItem value="resolved">已解决</SelectItem></SelectGroup></SelectContent></Select><div className="flex flex-wrap gap-2"><Button variant="outline" disabled={busy} onClick={exportAll}><Download data-icon="inline-start" />{busy ? "正在导出…" : "完整导出"}</Button><Button onClick={() => { setEditing(null); setOpen(true) }}><Plus data-icon="inline-start" />记录问题</Button></div></div>
    {error && <Alert variant="destructive"><AlertDescription>{error}<Button className="ml-2" variant="outline" onClick={() => setAttempt(value => value + 1)}><RefreshCw data-icon="inline-start" />重试</Button></AlertDescription></Alert>}
    <p className="text-xs/relaxed text-muted-foreground">页面最多显示最近 200 条，完整导出包含全部记录。记录会持久保存；请勿填写真实患者信息。</p>
    {items === null ? error ? <EvidenceEmpty title="记录暂时无法读取" description="已有记录仍在数据库中，请重试。" /> : <div role="status" aria-label="正在读取问题记录"><Skeleton className="h-36 w-full" /></div> : visible.length ? <div className="divide-y border-y">{visible.map(item => <article key={item.id} className="flex flex-col gap-3 py-5" data-issue-id={item.id}>
      <div className="flex flex-wrap items-start justify-between gap-3"><div className="flex flex-col gap-2"><h2 className="text-base font-semibold">{item.title}</h2><div className="flex flex-wrap gap-2"><Badge variant={item.status === "resolved" ? "secondary" : "outline"}>{item.status === "resolved" ? "已解决" : "待处理"}</Badge><Badge variant="outline">{CATEGORY_LABELS[item.category]}</Badge></div></div><Button variant="outline" onClick={() => { setEditing(item); setOpen(true) }}>更新记录</Button></div>
      <dl className="grid gap-3 text-sm/relaxed md:grid-cols-2"><div><dt className="text-xs text-muted-foreground">实际观察</dt><dd className="mt-1 whitespace-pre-wrap break-words">{item.observation}</dd></div><div><dt className="text-xs text-muted-foreground">预期表现</dt><dd className="mt-1 whitespace-pre-wrap break-words">{item.expected || "未填写"}</dd></div></dl>
      <div className="text-sm/relaxed"><h3 className="text-xs text-muted-foreground">处理与经验</h3><p className="mt-1 whitespace-pre-wrap break-words">{item.lesson || "尚未记录"}</p></div>
      <p className="break-all text-xs text-muted-foreground">更新于 {formatDate(item.updated_at)}{item.run_id ? ` · 关联检索 ${item.run_id}` : ""}</p>
    </article>)}</div> : <EvidenceEmpty title="当前状态下没有问题记录" description="可以记录本次使用中发现的问题，保存后再补充处理经验。" />}
    <IssueForm open={open} issue={editing} onClose={() => setOpen(false)} onSaved={() => { setOpen(false); setAttempt(value => value + 1) }} />
  </AppShell>
}
