import { useEffect, useRef, useState } from "react"
import { Loader2, MessageSquare, Search } from "lucide-react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { filterKind, EMPTY_FIELDS, extractCase, searchEvidence, type CaseFields, type DemoCase, type Evidence, type SearchResult } from "@/lib/evidence"
import { EvidenceEmpty, EvidenceRows, KindSelect } from "./EvidenceList"

const FIELDS: { key: keyof CaseFields; label: string; max: number }[] = [
  { key: "cancer", label: "癌种", max: 100 }, { key: "stage", label: "分期", max: 50 },
  { key: "biomarker", label: "分子特征", max: 200 }, { key: "prior_therapy", label: "既往治疗", max: 300 },
  { key: "city", label: "城市", max: 100 },
]
export function CaseSearch({ examples, onSelect, onIssue, onInvalidate }: {
  examples: DemoCase[]; onSelect: (item: Evidence) => void; onIssue: (run: string | null) => void; onInvalidate: () => void
}) {
  const [text, setText] = useState("")
  const [exampleId, setExampleId] = useState("SYNTHETIC-001")
  const [fields, setFields] = useState<CaseFields>({ ...EMPTY_FIELDS })
  const [evidence, setEvidence] = useState<Partial<CaseFields>>({})
  const [warnings, setWarnings] = useState<string[]>([])
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState<"extract" | "search" | null>(null)
  const [error, setError] = useState("")
  const [result, setResult] = useState<SearchResult | null>(null)
  const [kind, setKind] = useState("all")
  const revision = useRef(0)
  useEffect(() => () => { revision.current += 1 }, [])
  function invalidate() {
    revision.current += 1
    setConfirmed(false); setResult(null); setBusy(null); setError(""); onInvalidate()
  }
  function changeText(value: string) {
    invalidate(); setText(value); setFields({ ...EMPTY_FIELDS }); setEvidence({}); setWarnings([])
  }
  function changeField(key: keyof CaseFields, value: string) {
    invalidate(); setFields(prev => ({ ...prev, [key]: value })); setEvidence(prev => ({ ...prev, [key]: undefined }))
  }
  async function extract() {
    invalidate(); setFields({ ...EMPTY_FIELDS }); setEvidence({}); setWarnings([])
    const current = revision.current; setBusy("extract")
    try {
      const data = await extractCase(text)
      if (current !== revision.current) return
      setFields(data.fields); setEvidence(data.evidence); setWarnings(data.warnings)
    } catch (e) { if (current === revision.current) setError((e as Error).message) }
    finally { if (current === revision.current) setBusy(null) }
  }
  async function search() {
    if (!confirmed) return
    const current = ++revision.current
    setResult(null); setBusy("search"); setError(""); onInvalidate()
    try {
      const data = await searchEvidence(fields)
      if (current === revision.current) { setResult(data); setKind("all") }
    } catch (e) { if (current === revision.current) setError((e as Error).message) }
    finally { if (current === revision.current) setBusy(null) }
  }
  const visible = filterKind(result?.results ?? [], kind)
  const excluded = filterKind(result?.excluded ?? [], kind)
  return <div className="grid items-start gap-6 xl:grid-cols-[minmax(20rem,0.85fr)_minmax(0,1.15fr)]">
    <section className="flex min-w-0 flex-col gap-5 rounded-lg border p-4 md:p-5" aria-labelledby="case-heading">
      <h2 id="case-heading" className="text-base font-semibold">核对检索条件</h2>
      <FieldGroup>
        <Field><FieldLabel htmlFor="demo-case">虚构病例</FieldLabel>
          <div className="flex flex-wrap gap-2"><Select value={exampleId} onValueChange={setExampleId}><SelectTrigger className="flex-1" aria-label="虚构示例"><SelectValue placeholder="选择虚构示例" /></SelectTrigger><SelectContent><SelectGroup>{examples.map((item, index) => <SelectItem key={item.id} value={item.id}>虚构示例 {index + 1}</SelectItem>)}</SelectGroup></SelectContent></Select>
            <Button variant="outline" disabled={!examples.length} onClick={() => changeText(examples.find(item => item.id === exampleId)?.text ?? "")}>使用示例</Button></div>
          <Textarea id="demo-case" className="min-h-28" placeholder="仅输入虚构病例；也可直接填写下方检索条件。" maxLength={6000} value={text} onChange={e => changeText(e.target.value)} />
          <div className="flex items-center justify-between gap-2"><span className="text-xs text-muted-foreground">{text.length} / 6000</span><Button variant="outline" onClick={extract} disabled={!text.trim() || busy !== null}>{busy === "extract" && <Loader2 data-icon="inline-start" className="animate-spin" />}提取字段</Button></div>
        </Field>
      </FieldGroup>
      {warnings.length > 0 && <Alert><AlertDescription>{warnings.join(" ")}</AlertDescription></Alert>}
      <FieldGroup className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {FIELDS.map(({ key, label, max }) => <Field key={key}><FieldLabel htmlFor={`case-${key}`}>{label}</FieldLabel><Input id={`case-${key}`} value={fields[key]} maxLength={max} placeholder="未知" onChange={e => changeField(key, e.target.value)} />
          {evidence[key] && <FieldDescription>提取依据：{evidence[key]}</FieldDescription>}
        </Field>)}
      </FieldGroup>
      <p className="text-xs/relaxed text-muted-foreground">空白字段保持未知；修改后需重新确认。病例原文与字段不保存。</p>
      <Field orientation="horizontal"><Checkbox id="case-confirmed" checked={confirmed} disabled={busy === "extract"} onCheckedChange={value => { if (value !== true) { invalidate() } else { setConfirmed(true) } }} /><FieldLabel htmlFor="case-confirmed">我已逐项核对字段</FieldLabel></Field>
      {error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}
      <Button className="h-10" onClick={search} disabled={!confirmed || busy !== null}>{busy === "search" ? <Loader2 data-icon="inline-start" className="animate-spin" /> : <Search data-icon="inline-start" />}查询资料</Button>
    </section>
    <section className="min-w-0" aria-labelledby="results-heading" aria-busy={busy === "search"}>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b pb-4"><div className="flex flex-col gap-1"><h2 id="results-heading" className="text-base font-semibold">检索结果</h2><p className="text-xs text-muted-foreground">仅作教学资料条件匹配。</p></div><KindSelect value={kind} onChange={setKind} /></div>
      {!result ? <EvidenceEmpty title={busy === "search" ? "正在检索资料…" : "核对条件后开始检索"} description="先提取或手动填写字段，再勾选确认。修改输入会清除旧结果。" /> : <>
        <p className="py-3 text-sm/relaxed" role="status">{result.message}</p>
        {visible.length ? <EvidenceRows items={visible} onSelect={onSelect} /> : <EvidenceEmpty title="没有匹配的教学条目" description={result.status === "insufficient" ? result.message : "可以调整资料类型或检查条件；这不代表没有相关治疗或试验。"} />}
        {excluded.length > 0 && <details className="rounded-lg border px-4"><summary className="cursor-pointer py-4 text-sm font-medium">条件冲突条目（{excluded.length}）</summary><EvidenceRows items={excluded} onSelect={onSelect} /></details>}
      </>}
      <Button variant="outline" className="mt-4" onClick={() => onIssue(result?.run_id ?? null)}><MessageSquare data-icon="inline-start" />记录本次问题</Button>
    </section>
  </div>
}
