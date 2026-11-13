import { useState, type FormEvent } from "react";
import { ArrowUpRight, Info } from "lucide-react";
import { AppShell } from "@/layouts/AppShell";
import { ErrorMessage, NoData } from "@/components/curation/Common";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ArticleSource, AnchorSource, FactFields, JsonDetails, KnowledgeStatusBadges } from "@/components/knowledge/KnowledgeRecordSources";
import { EvidenceContext, EvidenceLabels } from "@/components/knowledge/EvidencePresentation";
import { useCuration } from "@/lib/curation";
import {
  KNOWLEDGE_KINDS, knowledgeLabel, knowledgeQuery,
  type KnowledgeCandidate,
  type KnowledgeCandidateDetail, type KnowledgeEntity, type KnowledgeEntityDetail, type KnowledgePage,
} from "@/lib/knowledge-candidates";

const PAGE_SIZE = 20;
const STAGE_LABELS: Record<string, string> = {
  complete: "完成", partial: "部分完成", failed: "失败", stored: "已保存",
  unassessed: "未评估", unreviewed: "未核验", valid: "有效", invalid: "无效",
  undetermined: "待判定", ready: "可导出", pending_ids: "待补目标 ID",
  not_expressible: "目标暂不能表达", blocked: "存在适配缺口",
  compatible: "结构兼容", pending_mapping: "待目标映射", incompatible: "不兼容",
  offline_validated: "离线结构通过", not_representable: "目标暂不能表达",
  invalid_payload: "目标字段不合法", invalid_source: "来源不合法",
  legacy_pass: "历史标准通过", legacy_fail: "历史标准未通过",
};

const ELIGIBILITY_REASONS: Record<string, string> = {
  glossary_context_only: "术语表内容，仅保留作上下文",
  no_extracted_fact: "尚未抽出事实",
  no_core_relation: "尚未形成明确的核心关系",
  methods_only_results: "仅描述分析方法，尚未给出观察结果",
  no_located_participants: "尚未定位关系参与实体",
  target_anchor_not_cited: "未引用本次选定的中心段落",
  invalid_evidence: "原文引用尚不可用",
  model_output_not_complete: "模型输出不完整",
  unlocated_participant: "部分关系参与实体未定位",
  invalid_relation: "关系类型尚不可用",
  missing_entities: "关系参与实体缺失",
  invalid_entity_roles: "实体角色尚不可用",
};

function EligibilityReasons({ item }: { item: KnowledgeCandidate }) {
  if (item.eligibility?.status !== "deferred") return null;
  return <p className="break-words text-xs/relaxed text-muted-foreground" aria-label="结构待处理原因">{item.eligibility.reasons.map((reason) => ELIGIBILITY_REASONS[reason] ?? "需检查提取结构").join("；")}。原始提取和出处仍保留。</p>;
}

function ExtractionStates({ item }: { item: KnowledgeCandidate }) {
  const stages = [
    ["提取", item.execution?.status ?? "stored"],
    ["目标适配", item.compatibility?.status ?? "unassessed"],
    ["独立核验", item.quality?.status ?? "unreviewed"],
  ];
  return <div className="flex flex-wrap gap-2" aria-label="关系结构、提取、适配与核验状态">
    <Badge variant="outline">{item.eligibility?.status === "eligible" ? "已形成关系结构" : item.eligibility?.status === "deferred" ? "尚未形成有效知识结构" : "关系结构未评估"}</Badge>
    {stages.map(([label, status]) => <Badge key={label} variant={status === "invalid" || status === "failed" ? "destructive" : "outline"}>{label}：{STAGE_LABELS[status] ?? status}</Badge>)}
  </div>;
}

function Pagination({ count, offset, setOffset }: { count: number; offset: number; setOffset: (value: number) => void }) {
  return <div className="flex flex-wrap items-center justify-between gap-2">
    <p className="text-xs text-muted-foreground">共 {count} 条{count ? ` · 当前 ${offset + 1}–${Math.min(offset + PAGE_SIZE, count)}` : ""}</p>
    <div className="flex gap-2">
      <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</Button>
      <Button size="sm" variant="outline" disabled={offset + PAGE_SIZE >= count} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</Button>
    </div>
  </div>;
}

function CandidateRows({ rows, onSelect }: { rows: KnowledgeCandidate[]; onSelect: (id: string) => void }) {
  return <div className="flex min-w-0 flex-col gap-3">{rows.map((row) => <article key={row.id} className="flex min-w-0 flex-col gap-2 rounded-lg border p-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <strong>{row.group_id}</strong><ExtractionStates item={row} />
    </div>
    <p className="break-words text-sm/relaxed">{row.article.title}</p>
    <EvidenceLabels data={row.evidence_presentation} />
    <EligibilityReasons item={row} />
    {row.target_readiness?.reason && <p className="text-xs/relaxed text-muted-foreground">目标适用范围：{row.target_readiness.reason}</p>}
    <p className="text-xs text-muted-foreground">PMID {row.article.pmid} · {row.entity_count} 个词条 · {row.anchor_count} 处原文定位</p>
    {row.relationship_participants?.length ? <p className="break-words text-xs/relaxed">参与本条关系：{row.relationship_participants.map((participant) => `${knowledgeLabel(participant.role)} ${participant.label}`).join(" · ")}</p> : null}
    {typeof row.fact_summary?.conclusion === "string" ? <p lang="en" className="break-words text-sm/relaxed">{row.fact_summary.conclusion}</p> : null}
    {row.current_review_id ? <KnowledgeStatusBadges item={row} /> : null}
    <div><Button size="sm" variant="outline" onClick={() => onSelect(row.id)}>查看 {row.group_id} 知识详情</Button></div>
  </article>)}</div>;
}

function EntityDetail({ entity, onCandidate }: { entity: KnowledgeEntityDetail; onCandidate: (id: string) => void }) {
  const sourceRecords = new Map<string, {
    candidate_id: string; entity_ref: string; attributes: Record<string, unknown>[];
  }>();
  for (const anchor of entity.anchors) {
    const key = `${anchor.candidate_id}:${anchor.entity_ref}`;
    let record = sourceRecords.get(key);
    if (!record) {
      record = { candidate_id: anchor.candidate_id, entity_ref: anchor.entity_ref, attributes: [] };
      sourceRecords.set(key, record);
    }
    // One source mention can have several positions or a separate definition.
    // Keep distinct metadata without repeating the same link at every position.
    if (!record.attributes.some((value) => JSON.stringify(value) === JSON.stringify(anchor.attributes))) {
      record.attributes.push(anchor.attributes);
    }
  }
  return <Card className="min-w-0" aria-label="词条详情">
    <CardHeader>
      <CardTitle><span lang="en">{entity.label}</span></CardTitle>
      <CardDescription>{knowledgeLabel(entity.kind)}{entity.subtype ? ` · ${knowledgeLabel(entity.subtype)}` : ""} · 候选词条</CardDescription>
    </CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-5">
      <p className="text-sm/relaxed">原文词条按文章版本保留。下面的关联说明它参与哪条候选或出现在何处，不代表标准身份已确认或疗效已成立。</p>
      <ArticleSource article={entity.article} />
      <section className="flex min-w-0 flex-col gap-3"><h3 className="font-medium">关联知识（{entity.candidate_count}）</h3><CandidateRows rows={entity.candidates} onSelect={onCandidate} />
        {entity.candidates_truncated ? <p className="text-xs text-muted-foreground">当前显示前 100 条关联知识。</p> : null}
      </section>
      <section className="flex min-w-0 flex-col gap-4" aria-label="词条英文原文">
        <h3 className="font-medium">原文提及与定义（{entity.anchor_count} 处）</h3>
        {entity.anchors.map((anchor) => <AnchorSource key={anchor.id} anchor={anchor} />)}
        {entity.anchors_truncated ? <p className="text-xs text-muted-foreground">当前显示前 100 处原文定位。</p> : null}
      </section>
      <JsonDetails title="查看候选标准关联与类型记录" value={{
        entity_attributes: entity.attributes,
        source_mentions: [...sourceRecords.values()],
        source_mentions_truncated: entity.anchors_truncated,
      }} />
    </CardContent>
    <CardFooter><p className="break-all text-xs text-muted-foreground">本地词条 ID：{entity.id}</p></CardFooter>
  </Card>;
}

function CandidateDetail({ candidate, onEntity }: { candidate: KnowledgeCandidateDetail; onEntity: (id: string) => void }) {
  const evidence = candidate.anchors.filter((anchor) => anchor.purpose === "evidence");
  const [mentionsOpen, setMentionsOpen] = useState(false);
  return <Card className="min-w-0" aria-label="知识候选详情">
    <CardHeader>
      <div className="flex flex-wrap items-center justify-between gap-2"><CardTitle>{candidate.group_id} · {candidate.eligibility?.status === "deferred" ? "原始提取记录" : "知识候选"}</CardTitle><ExtractionStates item={candidate} /></div>
      <CardDescription>关系结构、提取执行、目标适配和独立核验分别记录。形成结构不等于内容已核验正确，原始记录与出处始终保留。</CardDescription>
    </CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-5">
      <EligibilityReasons item={candidate} />
      {typeof candidate.assessment.brief === "string" ? <p className="text-sm/relaxed"><strong>既有参考核验：</strong>{candidate.assessment.brief}</p> : null}
      {candidate.quality?.reason ? <p className="text-sm/relaxed"><strong>独立核验结果：</strong>{candidate.quality.reason}</p> : null}
      {candidate.target_readiness?.reason ? <p className="text-sm/relaxed"><strong>目标适用范围：</strong>{candidate.target_readiness.reason} 原始适配记录与知识均保留。</p> : null}
      {candidate.quality?.quality_dimensions ? <p className="text-xs/relaxed" aria-label="独立质量维度">核心知识有效：{candidate.quality.quality_dimensions.core_valid ? "是" : "否"} · 目标知识已覆盖：{candidate.quality.quality_dimensions.target_covered ? "是" : "否"} · 关系参与实体可用：{candidate.quality.quality_dimensions.participants_usable ? "是" : "否"}</p> : null}
      <ArticleSource article={candidate.article} />
      <EvidenceContext data={candidate.evidence_presentation} />
      {candidate.normalized_participants?.length ? <section className="flex min-w-0 flex-col gap-3" aria-label="本地实体归一">
        <h3 className="font-medium">本地实体归一</h3>
        <p className="text-xs text-muted-foreground">已确认名称用于复用和检索。原文中的亚型、部位及其他限定仍保留，归到主癌种不代表扩大适用范围。</p>
        {candidate.normalized_participants.map((participant) => <article key={participant.entity_ref} className="flex min-w-0 flex-col gap-2 rounded-lg border p-3">
          <p className="break-words text-sm"><span lang="en">{participant.original_label}</span>{" → "}{participant.state === "resolved" && participant.canonical ? participant.canonical.name : participant.state === "ignored" ? "不作为标准实体" : "尚待对齐"}</p>
          {participant.state === "resolved" && participant.canonical ? <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline">{participant.canonical.entity_level === "drug_class" ? "药物类别，未指明具体成分" : participant.canonical.level === "main" ? "主癌种" : participant.canonical.level === "subtype" ? "亚型" : knowledgeLabel(participant.canonical.kind)}</Badge>
            {participant.canonical.main_cancer ? <span className="text-xs text-muted-foreground">主癌种：{participant.canonical.main_cancer.name}</span> : null}
            <Button size="sm" variant="outline" asChild><a href={`/knowledge/entities/?view=${participant.canonical.kind === "drug" ? "drugs" : "diseases"}&entity=${encodeURIComponent(participant.canonical.id)}`}>查看标准实体</a></Button>
          </div> : null}
          <JsonDetails title="查看保留的限定信息" value={participant.qualifiers} />
        </article>)}
      </section> : null}
      <section className="flex min-w-0 flex-col gap-3"><h3 className="font-medium">已有模型抽取字段</h3><p className="text-xs text-muted-foreground">以下保留模型结果，属于结构化整理；英文证据在下方原文中核对。药名字段中的治疗类别按词条类型另列，不替代具体药名。</p><FactFields fact={candidate.fact} />
        {typeof candidate.fact.conclusion === "string" ? <p lang="en" className="whitespace-pre-wrap break-words text-sm/relaxed"><strong>结论：</strong>{candidate.fact.conclusion}</p> : null}
        {typeof candidate.fact.direction === "string" ? <p className="text-xs">关系方向：{candidate.fact.direction}</p> : null}
        {candidate.fact.results ? <JsonDetails title="展开结局与数值" value={candidate.fact.results} /> : null}
        <JsonDetails title="查看完整原始关系结构" value={candidate.fact} />
      </section>
      <section className="flex min-w-0 flex-col gap-2" aria-label="关联实体与角色">
        <h3 className="font-medium">实体与关联依据</h3>
        <p className="text-xs text-muted-foreground">类型表示“是什么”；角色表示它在本条候选中的用途，仍待证据核验。原词匹配只建立可追溯关联，不推导疗效或因果。</p>
        <Table><TableHeader><TableRow><TableHead>英文原词</TableHead><TableHead>类型</TableHead><TableHead>关联依据／角色</TableHead><TableHead>操作</TableHead></TableRow></TableHeader>
          <TableBody>{candidate.entities.map((entity) => {
            const anchors = candidate.anchors.filter((anchor) => anchor.entity_id === entity.id);
            const associations = [...new Set(anchors.map((anchor) => anchor.association))];
            const roles = [...new Set(anchors.flatMap((anchor) => anchor.roles))];
            return <TableRow key={entity.id}><TableCell className="max-w-md whitespace-normal break-words"><span lang="en">{entity.label}</span></TableCell><TableCell>{knowledgeLabel(entity.kind)}</TableCell>
              <TableCell className="whitespace-normal"><div className="flex flex-col gap-1">{associations.map((value) => <span key={value}>{knowledgeLabel(value)}</span>)}{roles.length ? <span className="text-xs text-muted-foreground">用途：{roles.map(knowledgeLabel).join("、")}</span> : null}</div></TableCell>
              <TableCell><Button size="sm" variant="outline" onClick={() => onEntity(entity.id)} aria-label={`查看 ${entity.label} 词条详情`}>词条详情</Button></TableCell></TableRow>;
          })}</TableBody>
        </Table>
      </section>
      <section aria-label="关系英文原文" className="flex min-w-0 flex-col gap-4"><h3 className="font-medium">关系引用的英文原文（{evidence.length} 处）</h3>
        {evidence.length ? evidence.map((anchor) => <AnchorSource key={anchor.id} anchor={anchor} />) : <p className="text-sm text-muted-foreground">没有已保存的关系引用。</p>}
      </section>
      <details className="min-w-0" onToggle={(event) => setMentionsOpen(event.currentTarget.open)}><summary className="cursor-pointer text-sm">展开实体提及、定义和上下文定位</summary>
        {mentionsOpen ? <div className="mt-3 flex min-w-0 flex-col gap-4">{candidate.anchors.filter((anchor) => anchor.purpose !== "evidence").map((anchor) => <AnchorSource key={anchor.id} anchor={anchor} />)}</div> : null}
      </details>
      <JsonDetails title="查看既有参考核验及尚未解决的问题" value={candidate.assessment} />
      <JsonDetails title="查看结构、提取、目标适配及独立核验记录" value={{ eligibility: candidate.eligibility, execution: candidate.execution, compatibility: candidate.compatibility, quality: candidate.quality }} />
      <JsonDetails title="查看本次收纳来源与未决项" value={candidate.provenance} />
    </CardContent>
    <CardFooter className="flex flex-col items-start gap-3">
      {candidate.current_review_id ? <Button size="sm" variant="outline" asChild><a href={`/knowledge/search/?knowledge=${encodeURIComponent(candidate.id)}`}>查看本轮核验与发布状态 <ArrowUpRight data-icon="inline-end" /></a></Button> : null}
      <p className="break-all text-xs text-muted-foreground">入库 ID：{candidate.id} · 来源批次：{candidate.source_run}</p>
    </CardFooter>
  </Card>;
}

export default function KnowledgeCandidatesApp() {
  const initial = new URLSearchParams(location.search);
  const [view, setView] = useState(initial.has("entity") || initial.has("kind") ? "entities" : "candidates");
  const [kind, setKind] = useState(initial.get("kind") ?? "all");
  const [search, setSearch] = useState(initial.get("q") ?? "");
  const [query, setQuery] = useState(search);
  const [relationInput, setRelationInput] = useState({ disease: initial.get("disease") ?? "", drug: initial.get("drug") ?? "", biomarker: initial.get("biomarker") ?? "" });
  const [relationQuery, setRelationQuery] = useState(relationInput);
  const [offset, setOffset] = useState(0);
  const [entityId, setEntityId] = useState(initial.get("entity") ?? "");
  const [candidateId, setCandidateId] = useState(initial.get("candidate") ?? "");
  const [sourceRun] = useState(initial.get("source_run") ?? "");
  const filters = knowledgeQuery({ q: query, kind: view === "entities" && kind !== "all" ? kind : "", ...(view === "candidates" ? relationQuery : {}), source_run: sourceRun, limit: PAGE_SIZE, offset });
  const entities = useCuration<KnowledgePage<KnowledgeEntity>>(view === "entities" ? `knowledge-entities/?${filters}` : null);
  const candidates = useCuration<KnowledgePage<KnowledgeCandidate>>(view === "candidates" ? `knowledge-candidates/?${filters}` : null);
  const entity = useCuration<KnowledgeEntityDetail>(entityId ? `knowledge-entities/${encodeURIComponent(entityId)}/` : null);
  const candidate = useCuration<KnowledgeCandidateDetail>(candidateId ? `knowledge-candidates/${encodeURIComponent(candidateId)}/` : null);
  const list = view === "entities" ? entities : candidates;

  function selectDetail(key: "entity" | "candidate", id: string) {
    setEntityId(key === "entity" ? id : "");
    setCandidateId(key === "candidate" ? id : "");
    const url = new URL(location.href);
    url.searchParams.delete("entity"); url.searchParams.delete("candidate");
    if (id) url.searchParams.set(key, id);
    history.replaceState(null, "", url);
    if (id) requestAnimationFrame(() => document.getElementById("knowledge-detail")?.scrollIntoView({ block: "start", behavior: "smooth" }));
  }

  function submit(event: FormEvent) {
    event.preventDefault(); setQuery(search.trim());
    setRelationQuery({ disease: relationInput.disease.trim(), drug: relationInput.drug.trim(), biomarker: relationInput.biomarker.trim() });
    setOffset(0);
  }

  return <AppShell title="提取知识" description="按同一条知识中的癌种、药物和标志物检索，回查文章与英文原文。">
    <div className="flex min-w-0 flex-col gap-5">
      <Alert><Info /><AlertTitle>知识提取与质量核验独立</AlertTitle><AlertDescription>这里展示已保存的提取结果，包含尚未核验的知识。癌种、药物和标志物按同一条关系中的明确角色组合查询；原文中仅被提及的实体不作为关系检索命中。</AlertDescription></Alert>
      {entityId || candidateId ? <section id="knowledge-detail" className="flex min-w-0 scroll-mt-20 flex-col gap-3" aria-label="当前详情">
        <div><Button size="sm" variant="outline" onClick={() => selectDetail("entity", "")}>关闭详情</Button></div>
        {entity.error || candidate.error ? <ErrorMessage error={entity.error || candidate.error} /> : null}
        {entity.loading || candidate.loading ? <Skeleton className="h-48 w-full" /> : null}
        {entityId && !entity.loading && entity.data ? <EntityDetail key={entity.data.id} entity={entity.data} onCandidate={(id) => selectDetail("candidate", id)} /> : null}
        {candidateId && !candidate.loading && candidate.data ? <CandidateDetail key={candidate.data.id} candidate={candidate.data} onEntity={(id) => selectDetail("entity", id)} /> : null}
      </section> : null}
      <Card className="min-w-0">
        <CardHeader><CardTitle>浏览词条与知识</CardTitle><CardDescription>选择类型或搜索原词，点击“详情”查看关联和出处。</CardDescription></CardHeader>
        <CardContent className="flex min-w-0 flex-col gap-4">
          <Tabs value={view} onValueChange={(value) => { setView(value); setOffset(0); }}><TabsList><TabsTrigger value="candidates">提取知识</TabsTrigger><TabsTrigger value="entities">实体词条</TabsTrigger></TabsList></Tabs>
          <form onSubmit={submit}>
            <FieldGroup className="flex flex-col gap-3 sm:flex-row sm:items-end">
              {view === "entities" ? <Field className="sm:w-44 sm:shrink-0"><FieldLabel htmlFor="knowledge-kind">词条类型</FieldLabel><Select value={kind} onValueChange={(value) => { setKind(value); setOffset(0); }}><SelectTrigger id="knowledge-kind"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="all">全部类型</SelectItem>{Object.entries(KNOWLEDGE_KINDS).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectGroup></SelectContent></Select></Field> : null}
              <Field><FieldLabel htmlFor="knowledge-search">{view === "entities" ? "查找英文原词" : "查找组号或文章"}</FieldLabel><Input id="knowledge-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder={view === "entities" ? "例如 ICI、CRP kinetics" : "例如 K08 或 PMID"} /></Field>
              <Button type="submit" className="sm:shrink-0">查找</Button>
            </FieldGroup>
            {view === "candidates" ? <div className="mt-3 flex flex-col gap-2"><FieldGroup className="grid grid-cols-1 gap-3 md:grid-cols-3">{([['disease', '癌种', '例如 乳腺癌'], ['drug', '药物／治疗', '例如 paclitaxel'], ['biomarker', '标志物／分子条件', '例如 CRP']] as const).map(([key, label, placeholder]) => <Field key={key}><FieldLabel htmlFor={`knowledge-${key}`}>{label}</FieldLabel><Input id={`knowledge-${key}`} value={relationInput[key]} onChange={(event) => setRelationInput((previous) => ({ ...previous, [key]: event.target.value }))} placeholder={placeholder} /></Field>)}</FieldGroup><p className="text-xs text-muted-foreground">多个条件按 AND 匹配同一条知识。癌种和药物支持原词及已确认的标准名称、别名和主癌种；药物只匹配实际干预，背景提及或对照不计入。未明确参与角色的旧记录仍可通过组号、文章或原词查找。</p></div> : null}
          </form>
          {list.error ? <ErrorMessage error={list.error} /> : null}
          {list.loading ? <Skeleton className="h-36 w-full" /> : null}
          {!list.loading && list.data?.count === 0 ? <NoData title="没有匹配的记录" description="可调整类型或关键词；未定位词条保留在对应知识的未决项中。" /> : null}
          {!list.loading && view === "entities" && entities.data?.results.length ? <Table aria-label="候选实体词条"><TableHeader><TableRow><TableHead>英文原词</TableHead><TableHead>类型</TableHead><TableHead>关联知识</TableHead><TableHead>操作</TableHead></TableRow></TableHeader><TableBody>{entities.data.results.map((row) => <TableRow key={row.id}><TableCell className="max-w-md whitespace-normal break-words"><span lang="en">{row.label}</span><span className="mt-1 block text-xs text-muted-foreground">候选词条 · 标准身份未确认</span></TableCell><TableCell>{knowledgeLabel(row.kind)}{row.subtype ? <span className="block text-xs text-muted-foreground">{knowledgeLabel(row.subtype)}</span> : null}</TableCell><TableCell>{row.candidate_count} 条</TableCell><TableCell><Button size="sm" variant="outline" onClick={() => selectDetail("entity", row.id)} aria-label={`查看 ${row.label} 词条详情`}>详情</Button></TableCell></TableRow>)}</TableBody></Table> : null}
          {!list.loading && view === "candidates" && candidates.data?.results.length ? <CandidateRows rows={candidates.data.results} onSelect={(id) => selectDetail("candidate", id)} /> : null}
          {list.data ? <Pagination count={list.data.count} offset={offset} setOffset={setOffset} /> : null}
        </CardContent>
        <CardFooter><Button variant="outline" size="sm" asChild><a href="/management/research/">查看研究历程 <ArrowUpRight data-icon="inline-end" /></a></Button></CardFooter>
      </Card>

    </div>
  </AppShell>;
}
