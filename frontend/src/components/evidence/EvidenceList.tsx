import { BookOpen, ChevronRight, Dna, Info } from "lucide-react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Empty, EmptyHeader, EmptyTitle, EmptyDescription } from "@/components/ui/empty"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from "@/components/ui/sheet"
import { Separator } from "@/components/ui/separator"
import { KIND_LABELS, geneMention, type Evidence } from "@/lib/evidence"

export function DemoNotice() {
  return <Alert><Info /><AlertDescription>教学资料 · 当前为自建样例，不用于诊疗或试验入组判断。仅使用虚构病例。</AlertDescription></Alert>
}
export function KindSelect({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return <Select value={value} onValueChange={onChange}><SelectTrigger aria-label="资料类型" className="w-36"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
    <SelectItem value="all">全部类型</SelectItem>
    {Object.entries(KIND_LABELS).map(([key, label]) => <SelectItem key={key} value={key}>{label}</SelectItem>)}
  </SelectGroup></SelectContent></Select>
}
export function EvidenceEmpty({ title, description }: { title: string; description: string }) {
  return <Empty className="min-h-48"><EmptyHeader><EmptyTitle>{title}</EmptyTitle><EmptyDescription>{description}</EmptyDescription></EmptyHeader></Empty>
}
export function EvidenceRows({ items, onSelect }: { items: Evidence[]; onSelect: (item: Evidence) => void }) {
  return <div className="divide-y">{items.map(item => <article key={item.id} className="flex flex-col gap-3 py-5" data-evidence-id={item.id}>
    <div className="flex flex-wrap items-center gap-2"><Badge variant="outline">{KIND_LABELS[item.kind]}</Badge><Badge variant="secondary">教学</Badge>
      {item.match_state && <span className="text-xs text-muted-foreground">{item.match_state === "excluded" ? "存在条件冲突" : item.match_state === "needs_review" ? "有条件待核对" : "教学条件匹配"}</span>}
    </div>
    <button type="button" onClick={() => onSelect(item)} className="w-fit text-left text-base font-semibold text-primary underline-offset-4 hover:underline">{item.title}</button>
    <p className="text-sm/relaxed text-muted-foreground">{item.summary}</p>
    <p className="text-xs/relaxed">{[item.cancer, item.stage ? `${item.stage} 期` : "", item.biomarker, item.city].filter(Boolean).join(" · ")}</p>
    {item.unknown?.length ? <div className="text-sm/relaxed"><span className="font-medium">待核对：</span>{item.unknown.join("；")}</div> : null}
    {item.conflicts?.length ? <div className="text-sm/relaxed"><span className="font-medium">条件冲突：</span>{item.conflicts.join("；")}</div> : null}
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="text-xs text-muted-foreground">{item.source_title} · {item.source_version}</span>
      <Button variant="outline" onClick={() => onSelect(item)}><BookOpen data-icon="inline-start" />查看出处</Button>
    </div>
  </article>)}</div>
}
export function SourceSheet({ item, onClose }: { item: Evidence | null; onClose: () => void }) {
  const gene = item ? geneMention(item) : null
  return <Sheet open={!!item} onOpenChange={open => { if (!open) onClose() }}><SheetContent className="overflow-y-auto data-[side=right]:w-full data-[side=right]:sm:max-w-xl">
    <SheetHeader><SheetTitle>{item?.title ?? "资料出处"}</SheetTitle><SheetDescription>核对原文、版本和条件。全部内容为教学资料。</SheetDescription></SheetHeader>
    {item && <div className="flex flex-col gap-5 px-5 pb-8 text-sm/relaxed">
      <div className="flex gap-2"><Badge variant="outline">{KIND_LABELS[item.kind]}</Badge><Badge variant="secondary">教学样例</Badge></div>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 break-words">
        <dt className="text-muted-foreground">来源</dt><dd>{item.source_title}</dd>
        <dt className="text-muted-foreground">版本</dt><dd>{item.source_version}</dd>
        <dt className="text-muted-foreground">定位</dt><dd>{item.source_locator}</dd>
        <dt className="text-muted-foreground">更新时间</dt><dd>{item.updated_at}</dd>
      </dl>
      <Separator /><section><h3 className="mb-2 font-semibold">原文摘录</h3><blockquote className="border-l-2 pl-4 leading-7">{item.source_text}</blockquote></section>
      {([['已匹配条件', item.matched], ['待核对条件', item.unknown], ['条件冲突', item.conflicts]] as [string, string[] | undefined][]).map(([label, notes]) => notes?.length ? <section key={label}><h3 className="mb-2 font-semibold">{label}</h3><ul className="flex list-disc flex-col gap-2 pl-5">{notes.map(note => <li key={note}>{note}</li>)}</ul></section> : null)}
      {gene && <div className="flex flex-col gap-2 border-t pt-4"><Button asChild variant="outline" className="w-fit"><a href={`/knowledge/genes/?q=${encodeURIComponent(gene)}`}><Dna data-icon="inline-start" />查看 {gene} 基因与转录本<ChevronRight data-icon="inline-end" /></a></Button><p className="text-xs text-muted-foreground">按资料中的基因文字查询，不表示变异或临床适用性已确认。</p></div>}
    </div>}
  </SheetContent></Sheet>
}
