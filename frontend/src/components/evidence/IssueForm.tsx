import { useState, type FormEvent } from "react"
import { Loader2 } from "lucide-react"
import { toast } from "sonner"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from "@/components/ui/sheet"
import { CATEGORY_LABELS, createIssue, updateIssue, type Issue, type IssueCategory } from "@/lib/evidence"

function IssueEditor({ issue, runId, onSaved }: { issue: Issue | null; runId: string | null; onSaved: () => void }) {
  const [title, setTitle] = useState("")
  const [category, setCategory] = useState<IssueCategory>("other")
  const [observation, setObservation] = useState("")
  const [expected, setExpected] = useState("")
  const [lesson, setLesson] = useState(issue?.lesson ?? "")
  const [status, setStatus] = useState<Issue["status"]>(issue?.status ?? "open")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("")
    try {
      if (issue) await updateIssue(issue.id, { status, lesson })
      else await createIssue({ title, category, observation, expected, lesson, run_id: runId })
      toast.success(issue ? "问题记录已更新" : "问题记录已保存"); onSaved()
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  return <form onSubmit={save} className="flex flex-col gap-5 px-5 pb-8">
    <FieldGroup>
      {issue ? <>
        <div className="flex flex-col gap-2 text-sm/relaxed"><h3 className="font-semibold">{issue.title}</h3><p className="whitespace-pre-wrap break-words">观察：{issue.observation}</p><p className="whitespace-pre-wrap break-words">预期：{issue.expected || "未填写"}</p></div>
        <Field><FieldLabel htmlFor="issue-status">处理状态</FieldLabel><Select value={status} onValueChange={value => setStatus(value as Issue["status"])}><SelectTrigger id="issue-status"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="open">待处理</SelectItem><SelectItem value="resolved">已解决</SelectItem></SelectGroup></SelectContent></Select></Field>
      </> : <>
        <Field><FieldLabel htmlFor="issue-title">问题标题</FieldLabel><Input id="issue-title" required minLength={2} maxLength={120} value={title} onChange={e => setTitle(e.target.value)} /></Field>
        <Field><FieldLabel htmlFor="issue-category">问题分类</FieldLabel><Select value={category} onValueChange={value => setCategory(value as IssueCategory)}><SelectTrigger id="issue-category"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{Object.entries(CATEGORY_LABELS).map(([key, label]) => <SelectItem key={key} value={key}>{label}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
        <Field><FieldLabel htmlFor="issue-observation">实际观察</FieldLabel><Textarea id="issue-observation" required minLength={2} maxLength={1500} value={observation} onChange={e => setObservation(e.target.value)} /><FieldDescription>请描述流程问题，不填写真实患者信息。</FieldDescription></Field>
        <Field><FieldLabel htmlFor="issue-expected">预期表现</FieldLabel><Textarea id="issue-expected" maxLength={1500} value={expected} onChange={e => setExpected(e.target.value)} /></Field>
      </>}
      <Field><FieldLabel htmlFor="issue-lesson">处理与经验</FieldLabel><Textarea id="issue-lesson" maxLength={1500} value={lesson} onChange={e => setLesson(e.target.value)} /></Field>
    </FieldGroup>
    {!issue && <p className="text-xs/relaxed text-muted-foreground">{runId ? "仅关联本次检索编号，不保存病例与字段。" : "独立问题记录，不关联病例或检索。"}</p>}
    {error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}
    <Button type="submit" disabled={busy} className="h-10">{busy && <Loader2 data-icon="inline-start" className="animate-spin" />}{issue ? "保存更新" : "保存问题"}</Button>
  </form>
}
export function IssueForm({ open, issue = null, runId = null, onClose, onSaved }: { open: boolean; issue?: Issue | null; runId?: string | null; onClose: () => void; onSaved: () => void }) {
  return <Sheet open={open} onOpenChange={value => { if (!value) onClose() }}><SheetContent className="overflow-y-auto data-[side=right]:w-full data-[side=right]:sm:max-w-lg"><SheetHeader><SheetTitle>{issue ? "更新问题" : "记录问题"}</SheetTitle><SheetDescription>记录提取、匹配、资料覆盖与使用中的实际问题。</SheetDescription></SheetHeader>{open && <IssueEditor key={issue?.id ?? "new"} issue={issue} runId={runId} onSaved={onSaved} />}</SheetContent></Sheet>
}
