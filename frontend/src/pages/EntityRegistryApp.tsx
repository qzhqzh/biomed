import { useState, type FormEvent } from "react";
import { ArrowRight, ChevronLeft, ChevronRight, Info, LogOut, Search, UserRound } from "lucide-react";
import { AppShell } from "@/layouts/AppShell";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { ErrorMessage, NoData } from "@/components/curation/Common";
import { curation, date, useCuration, type Session } from "@/lib/curation";
import { useUnsavedChanges } from "@/lib/navigation";
import {
  registryLabel, registryQuery,
  type RegistryEntity, type RegistryEntityDetail, type RegistryEvidence,
  type RegistryMention, type RegistryPage, type RegistryRule, type RegistryRuleDetail, type RegistrySummary,
} from "@/lib/entity-registry";

const PAGE_SIZE = 25;
const API = "entity-registry/";

function JsonDetails({ label, value }: { label: string; value: unknown }) {
  return <details className="min-w-0"><summary className="cursor-pointer text-sm text-muted-foreground">{label}</summary>
    <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-3 text-xs">{JSON.stringify(value, null, 2)}</pre>
  </details>;
}

function qualifierText(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(qualifierText).join("；");
  return JSON.stringify(value);
}

function Choice({ id, value, onChange, values }: { id: string; value: string; onChange: (value: string) => void; values: [string, string][] }) {
  return <Select value={value} onValueChange={onChange}><SelectTrigger id={id} className="w-full"><SelectValue /></SelectTrigger>
    <SelectContent><SelectGroup>{values.map(([key, label]) => <SelectItem key={key} value={key}>{label}</SelectItem>)}</SelectGroup></SelectContent>
  </Select>;
}

function StatusBadge({ value }: { value: string }) {
  return <Badge variant={value === "revoked" || value === "rejected" ? "outline" : "secondary"}>{registryLabel(value)}</Badge>;
}

function EvidenceList({ rows }: { rows: RegistryEvidence[] }) {
  return <div className="flex min-w-0 flex-col gap-3">{rows.map((row, index) => <div key={index} className="min-w-0 rounded-md border p-3">
    {typeof row.quote === "string" ? <p lang="en" className="whitespace-pre-wrap break-words text-sm/relaxed">{row.quote}</p> : <JsonDetails label="查看来源依据" value={row} />}
    <p className="mt-2 break-all text-xs text-muted-foreground">{row.group_id ? `知识 ${row.group_id} · ` : ""}{row.pmid ? `PMID ${row.pmid} · ` : ""}{row.locator || row.passage_id || "已保存来源记录"}</p>
  </div>)}</div>;
}

function KnowledgeContext({ value }: { value: Record<string, unknown> | undefined }) {
  if (!value) return null;
  const fields = [["population", "人群／疾病"], ["treatment", "干预"], ["comparator", "对照"], ["conditions", "限定条件"], ["scope", "适用范围"]];
  return <dl className="grid gap-2 text-sm/relaxed">{fields.map(([key, label]) => {
    const item = value[key];
    if (typeof item !== "string" && !Array.isArray(item)) return null;
    return <div key={key}><dt className="text-muted-foreground">{label}</dt><dd className="break-words">{Array.isArray(item) ? item.filter((part) => typeof part === "string").join("；") : item}</dd></div>;
  })}</dl>;
}

function Mentions({ rows }: { rows: RegistryMention[] }) {
  return <div className="flex min-w-0 flex-col gap-3">{rows.map((row, index) => <details key={row.id ?? index} className="min-w-0 rounded-md border p-3">
    <summary className="cursor-pointer break-words text-sm">{row.group_id || "知识引用"} · {row.original_label || row.label || "原词与上下文"}{row.article?.pmid ? ` · PMID ${row.article.pmid}` : ""}</summary>
    <div className="mt-3 flex min-w-0 flex-col gap-3">
      {row.article?.title ? <p className="text-sm/relaxed">{row.article.title}</p> : null}
      <KnowledgeContext value={row.context} />
      {row.candidate_id ? <Button size="sm" variant="outline" asChild><a href={`/knowledge/candidates/?candidate=${encodeURIComponent(row.candidate_id)}`}>查看完整知识与文章 <ArrowRight data-icon="inline-end" /></a></Button> : null}
      {row.anchors?.length ? <EvidenceList rows={row.anchors} /> : null}
      <JsonDetails label="查看保留的部位、分子限定及来源上下文" value={row.context ?? row} />
    </div>
  </details>)}</div>;
}

function EntityPicker({ kind, level = "", value, onChange, label }: {
  kind: string; level?: string; value: { id: string; name: string } | null;
  onChange: (entity: RegistryEntity | null) => void; label: string;
}) {
  const [input, setInput] = useState("");
  const [query, setQuery] = useState<string | null>(null);
  const list = useCuration<RegistryPage<RegistryEntity>>(query !== null ? `${API}entities/?${registryQuery({ kind, level, status: "active", q: query, limit: 20 })}` : null);
  return <Field>
    <FieldLabel>{label}</FieldLabel>
    {value ? <p className="break-words text-sm">当前选择：<strong>{value.name}</strong> <Button type="button" size="sm" variant="ghost" onClick={() => onChange(null)}>清除</Button></p> : <p className="text-xs text-muted-foreground">尚未选择</p>}
    <div className="flex gap-2"><Input aria-label={`搜索${label}`} placeholder={level === "main" ? "例如：脑胶质瘤" : "名称或别名"} value={input} onChange={(event) => setInput(event.target.value)} />
      <Button type="button" variant="outline" onClick={() => setQuery(input.trim())}><Search data-icon="inline-start" />查找</Button></div>
    <ErrorMessage error={list.error} />
    {list.loading ? <p role="status" className="text-xs text-muted-foreground">正在查找…</p> : null}
    {list.data ? <div className="flex max-h-52 flex-col gap-1 overflow-auto rounded-md border p-2" aria-label={`${label}候选`}>
      {list.data.results.length ? list.data.results.map((entity) => <Button type="button" variant="ghost" className="h-auto justify-start whitespace-normal py-2 text-left" key={entity.id} onClick={() => onChange(entity)}>
        {entity.name}{entity.name_en && entity.name_en !== entity.name ? ` · ${entity.name_en}` : ""} · {registryLabel(entity.level)}
      </Button>) : <p className="p-2 text-sm text-muted-foreground">没有符合条件的本地实体。</p>}
      {list.data.count > 20 ? <p className="p-2 text-xs text-muted-foreground">仅列前20项，请缩小名称范围。</p> : null}
    </div> : null}
  </Field>;
}

function RuleReview({ rule, onSaved }: { rule: RegistryRule; onSaved: (notice: string) => void }) {
  const [action, setAction] = useState(rule.status === "confirmed" ? "revoke" : "confirm");
  const [target, setTarget] = useState<{ id: string; name: string } | null>(rule.canonical_entity?.status === "active" ? rule.canonical_entity : null);
  const [name, setName] = useState(rule.alias);
  const [english, setEnglish] = useState("");
  const [level, setLevel] = useState(rule.kind === "disease" ? "unassigned" : "not_applicable");
  const [main, setMain] = useState<{ id: string; name: string } | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useUnsavedChanges(Boolean(reason.trim()), "实体审核还有未保存的说明。");
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const result = await curation<{ affected_mentions: number }>(`${API}rules/${rule.id}/review/`, {
        method: "POST", body: JSON.stringify({ expected_revision: rule.revision, action, reason,
          ...(action === "confirm" ? { canonical_id: target?.id } : {}),
          ...(action === "create" ? { name, name_en: english, level, main_id: level === "subtype" ? main?.id : null } : {}),
        }),
      });
      setReason(""); onSaved(`决定已保存，已处理 ${result.affected_mentions} 处登记引用；相同适用范围内可复用，原知识不变。`);
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }
  return <form onSubmit={submit} className="min-w-0"><FieldGroup>
    <Field><FieldLabel htmlFor="rule-action">处理方式</FieldLabel><Choice id="rule-action" value={action} onChange={setAction} values={rule.status === "confirmed" ? [["revoke", "撤销此条可复用规则"]] : [["confirm", rule.provenance?.match_relation === "qualified_form" ? "确认核心疾病与条件对应" : "对应已有实体／确认别名"], ["create", "新增本地实体并对应"], ["defer", "暂不确定"], ["reject", "拒绝此映射"]]} /></Field>
    {action === "confirm" && rule.action !== "ignore" ? <EntityPicker kind={rule.kind} value={target} onChange={setTarget} label="对应实体" /> : null}
    {action === "confirm" && rule.action === "ignore" ? <p className="text-sm text-muted-foreground">确认此名称不归入当前药物／疾病实体范围，原知识及引用仍保留。</p> : null}
    {action === "create" ? <>
      <Field><FieldLabel htmlFor="new-entity-name">新实体名称</FieldLabel><Input id="new-entity-name" value={name} onChange={(event) => setName(event.target.value)} required /></Field>
      <Field><FieldLabel htmlFor="new-entity-english">英文名称（可留空）</FieldLabel><Input id="new-entity-english" value={english} onChange={(event) => setEnglish(event.target.value)} /></Field>
      {rule.kind === "disease" ? <Field><FieldLabel htmlFor="new-entity-level">业务层级</FieldLabel><Choice id="new-entity-level" value={level} onChange={setLevel} values={[["unassigned", "层级待定"], ["main", "主癌种"], ["subtype", "亚型"], ["not_applicable", "非癌种／不适用"]]} /></Field> : null}
      {level === "subtype" ? <EntityPicker kind="disease" level="main" value={main} onChange={setMain} label="所属主癌种" /> : null}
    </> : null}
    <Field><FieldLabel htmlFor="rule-review-reason">依据或调整说明</FieldLabel><Textarea id="rule-review-reason" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="说明名称对应、分类或保留待定的依据" required /></Field>
    <ErrorMessage error={error} />
    <Button type="submit" disabled={busy || !reason.trim() || (action === "confirm" && rule.action !== "ignore" && !target) || (action === "create" && level === "subtype" && !main)}>{busy ? "正在保存…" : "保存实体决定"}</Button>
  </FieldGroup></form>;
}

function EntityReview({ entity, onSaved }: { entity: RegistryEntityDetail; onSaved: (notice: string) => void }) {
  const [action, setAction] = useState(entity.status === "candidate" ? "activate" : entity.kind === "disease" ? "set_classification" : "activate");
  const [name, setName] = useState(entity.name);
  const [english, setEnglish] = useState(entity.name_en);
  const [level, setLevel] = useState(entity.level);
  const [main, setMain] = useState<{ id: string; name: string } | null>(entity.main_cancer);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useUnsavedChanges(Boolean(reason.trim()), "实体分类还有未保存的说明。");
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const result = await curation<{ affected_mentions: number }>(`${API}entities/${entity.id}/review/`, { method: "POST", body: JSON.stringify({
        expected_revision: entity.revision, action, reason,
        ...(action === "rename" ? { name, name_en: english } : {}),
        ...(action === "set_classification" ? { level, main_id: level === "subtype" ? main?.id : null } : {}),
      }) });
      setReason(""); onSaved(`实体决定已保存，关联 ${result.affected_mentions} 处引用；部位、分子限定与原文仍保留。`);
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }
  const actions: [string, string][] = [["activate", "确认本地实体"], ["rename", "调整标准名称"], ["defer", "保留候选／待定"]];
  if (entity.kind === "disease") actions.unshift(["set_classification", "调整主癌种／亚型"]);
  return <form onSubmit={submit}><FieldGroup>
    <Field><FieldLabel htmlFor="entity-review-action">处理方式</FieldLabel><Choice id="entity-review-action" value={action} onChange={setAction} values={actions} /></Field>
    {action === "rename" ? <><Field><FieldLabel htmlFor="entity-review-name">标准名称</FieldLabel><Input id="entity-review-name" value={name} onChange={(event) => setName(event.target.value)} required /></Field>
      <Field><FieldLabel htmlFor="entity-review-en">英文名称</FieldLabel><Input id="entity-review-en" value={english} onChange={(event) => setEnglish(event.target.value)} /></Field></> : null}
    {action === "set_classification" ? <>
      <Field><FieldLabel htmlFor="entity-review-level">业务层级</FieldLabel><Choice id="entity-review-level" value={level} onChange={setLevel} values={[["main", "主癌种"], ["subtype", "亚型"], ["unassigned", "层级待定"], ["not_applicable", "非癌种／不适用"]]} /></Field>
      {level === "subtype" ? <EntityPicker kind="disease" level="main" value={main} onChange={setMain} label="所属主癌种" /> : null}
    </> : null}
    <Field><FieldLabel htmlFor="entity-review-reason">依据或调整说明</FieldLabel><Textarea id="entity-review-reason" value={reason} onChange={(event) => setReason(event.target.value)} required /></Field>
    <ErrorMessage error={error} />
    <Button disabled={busy || !reason.trim() || (action === "set_classification" && level === "subtype" && !main)}>{busy ? "正在保存…" : "保存分类／实体决定"}</Button>
  </FieldGroup></form>;
}

function EntityDetail({ entity, canReview, onRule, onEntity, onSaved }: { entity: RegistryEntityDetail; canReview: boolean; onRule: (rule: RegistryRule) => void; onEntity: (id: string) => void; onSaved: (notice: string) => void }) {
  return <Card className="min-w-0" aria-label="实体详情"><CardHeader><CardTitle>{entity.name}</CardTitle><CardDescription>{entity.name_en || "英文名称待补充"}</CardDescription></CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-5">
      <div className="flex flex-wrap gap-2"><StatusBadge value={entity.kind} /><StatusBadge value={entity.level} /><StatusBadge value={entity.status} /><StatusBadge value={entity.source_status} /></div>
      {entity.main_cancer ? <p className="text-sm">主癌种：<Button variant="link" className="h-auto p-0" onClick={() => onEntity(entity.main_cancer!.id)}>{entity.main_cancer.name}</Button> · 当前实体作为亚型保留</p> : null}
      <p className="text-sm text-muted-foreground">{entity.mention_count} 处登记引用 · {entity.alias_count} 个登记名称规则。身份对齐不改变知识关系及适用条件。</p>
      {entity.provenance ? <section className="flex flex-col gap-2 rounded-md bg-muted p-3 text-sm"><h3 className="font-medium">初始来源与分类依据</h3>
        <p>来源原名：{entity.provenance.source_name_cn || entity.provenance.source_name_en || entity.name}</p>
        {entity.kind === "disease" ? <p>来源血液肿瘤标记：{entity.provenance.is_blood_tumor === true ? "是" : entity.provenance.is_blood_tumor === false ? "否；其他类型尚未逐项重判" : "未知"}</p> : null}
        {entity.provenance.source_form === "possible_combination" ? <p>此名称可能包含多个药物／组合，需要确认组合关系，不能直接当作单一成分。</p> : null}
        {entity.provenance.classification_basis ? <p className="break-words">{entity.provenance.classification_basis}</p> : null}
        <JsonDetails label="查看保留的原名、来源别名与属性" value={entity.provenance} />
      </section> : null}
      {entity.children.length ? <section className="flex flex-col gap-2"><h3 className="font-medium">已归属的亚型</h3><div className="flex flex-wrap gap-2">{entity.children.map((child) => <Button size="sm" variant="outline" key={child.id} onClick={() => onEntity(child.id)}>{child.name}</Button>)}</div></section> : null}
      <section className="flex min-w-0 flex-col gap-2"><h3 className="font-medium">名称与可复用规则</h3>
        {entity.rules.length ? entity.rules.map((rule) => <div key={rule.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3"><div className="min-w-0"><p className="break-words text-sm">{rule.alias}</p>{rule.provenance?.match_relation === "qualified_form" ? <Badge variant="outline">核心疾病＋条件</Badge> : null}<p className="mt-1 text-xs text-muted-foreground">{registryLabel(rule.status)} · {registryLabel(rule.origin)} · {registryLabel(rule.scope.type)} · {rule.mention_count} 处引用</p></div><Button size="sm" variant="outline" onClick={() => onRule(rule)}>查看规则</Button></div>) : <p className="text-sm text-muted-foreground">尚无登记名称规则。</p>}
      </section>
      <section className="flex min-w-0 flex-col gap-3"><h3 className="font-medium">原词、知识与条件</h3><Mentions rows={entity.mentions} />{!entity.mentions.length ? <p className="text-sm text-muted-foreground">此基础条目尚未被本批知识引用。</p> : null}</section>
      <JsonDetails label="查看来源与外部ID（不用于在线拉取）" value={entity.external_ids} />
      {canReview ? <section className="flex flex-col gap-3 rounded-md border p-4"><h3 className="font-medium">实体与分类审核</h3><EntityReview key={`${entity.id}:${entity.revision}`} entity={entity} onSaved={onSaved} /></section> : <p className="text-sm text-muted-foreground">登录审阅者账号后可调整分类或确认实体。</p>}
      <details className="min-w-0"><summary className="cursor-pointer text-sm">决定历史（{entity.decisions.length}）</summary><div className="mt-3 flex flex-col gap-2">{entity.decisions.map((decision) => <div key={decision.id} className="rounded-md border p-3 text-sm"><p>{registryLabel(decision.action)} · {decision.actor || registryLabel(decision.origin || "rule")}{decision.created_at ? ` · ${date(decision.created_at)}` : ""}</p><p className="mt-1 whitespace-pre-wrap break-words">{decision.reason}</p></div>)}</div></details>
    </CardContent><CardFooter><p className="break-all text-xs text-muted-foreground">biomed ID：{entity.id} · 版本 {entity.revision}</p></CardFooter>
  </Card>;
}

function RuleDetail({ rule, canReview, onSaved, onEntity }: { rule: RegistryRuleDetail; canReview: boolean; onSaved: (notice: string) => void; onEntity: (id: string) => void }) {
  return <Card className="min-w-0" aria-label="名称规则详情"><CardHeader><CardTitle>{rule.alias}</CardTitle><CardDescription>按唯一名称和适用范围保存决定，不逐篇重复审核。</CardDescription></CardHeader><CardContent className="flex min-w-0 flex-col gap-4">
    <div className="flex flex-wrap gap-2"><StatusBadge value={rule.kind} /><StatusBadge value={rule.status} /><StatusBadge value={rule.provenance?.match_relation === "qualified_form" ? "qualified_form" : rule.action} /><StatusBadge value={rule.origin} /></div>
    <p className="text-sm">{rule.provenance?.match_relation === "qualified_form" ? "对应的核心疾病" : "对应实体"}：<strong>{rule.canonical_entity?.name || "尚未确定"}</strong> · 影响 {rule.mention_count} 处登记引用</p>
    {rule.provenance?.match_relation === "qualified_form" ? <Alert><Info /><AlertTitle>核心疾病＋条件</AlertTitle><AlertDescription><p>原词“{rule.alias}”包含额外限定，只关联核心疾病；此规则不表示原词与主癌种完全同义。阶段、部位、分子条件等继续随知识保留。</p>{rule.provenance.required_qualifiers ? <p className="break-words">必须保留的限定：{qualifierText(rule.provenance.required_qualifiers)}</p> : null}</AlertDescription></Alert> : null}
    {rule.canonical_entity ? <div><Button variant="outline" size="sm" onClick={() => onEntity(rule.canonical_entity!.id)}>{rule.canonical_entity.status === "candidate" ? "查看并确认候选实体" : "查看对应实体"}<ArrowRight data-icon="inline-end" /></Button></div> : null}
    <p className="break-all text-sm">复用范围：{registryLabel(rule.scope.type)}{rule.scope.key ? ` · ${rule.scope.key}` : ""}</p>
    <p className="whitespace-pre-wrap break-words text-sm/relaxed">{rule.reason || "尚未登记对应依据。"}</p>
    {rule.alternatives.length ? <JsonDetails label={`查看候选实体（${rule.alternatives.length}）`} value={rule.alternatives} /> : null}
    <section className="flex min-w-0 flex-col gap-2"><h3 className="font-medium">依据与原文</h3><EvidenceList rows={rule.evidence} /></section>
    {rule.mentions.length ? <section className="flex min-w-0 flex-col gap-3"><h3 className="font-medium">受此规则影响的知识</h3><Mentions rows={rule.mentions} /></section> : null}
    {canReview ? <section className="flex flex-col gap-3 rounded-md border p-4"><h3 className="font-medium">审核此规则</h3><RuleReview key={`${rule.id}:${rule.revision}`} rule={rule} onSaved={onSaved} /></section> : <p className="text-sm text-muted-foreground">登录后可确认对应、新增实体或保留待定。</p>}
    {rule.decisions.length ? <JsonDetails label={`查看决定历史（${rule.decisions.length}）`} value={rule.decisions} /> : null}
  </CardContent><CardFooter><p className="break-all text-xs text-muted-foreground">规则ID：{rule.id} · 版本 {rule.revision}</p></CardFooter></Card>;
}

export default function EntityRegistryApp() {
  const initial = new URLSearchParams(location.search);
  const [view, setView] = useState(["drugs", "diseases", "review"].includes(initial.get("view") || "") ? initial.get("view")! : "drugs");
  const [input, setInput] = useState(initial.get("q") || "");
  const [query, setQuery] = useState(input);
  const [level, setLevel] = useState("all");
  const [source, setSource] = useState("all");
  const [ruleStatus, setRuleStatus] = useState("pending");
  const [reviewKind, setReviewKind] = useState("all");
  const [reviewScope, setReviewScope] = useState("referenced");
  const [blood, setBlood] = useState("all");
  const [offset, setOffset] = useState(0);
  const [entityId, setEntityId] = useState(initial.get("entity") || "");
  const [ruleId, setRuleId] = useState(initial.get("rule") || "");
  const [notice, setNotice] = useState("");
  const [signIn, setSignIn] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [authError, setAuthError] = useState("");
  const session = useCuration<Session>("session/");
  const summary = useCuration<RegistrySummary>(`${API}summary/`);
  const entityList = useCuration<RegistryPage<RegistryEntity>>(view !== "review" ? `${API}entities/?${registryQuery({ kind: view === "drugs" ? "drug" : "disease", q: query, level: view === "diseases" && level !== "all" ? level : "", is_blood_tumor: view === "diseases" && blood !== "all" ? blood : "", source_status: source === "all" ? "" : source, limit: PAGE_SIZE, offset })}` : null);
  const ruleList = useCuration<RegistryPage<RegistryRule>>(view === "review" ? `${API}rules/?${registryQuery({ q: query, status: ruleStatus === "all" ? "" : ruleStatus, kind: reviewKind === "all" ? "" : reviewKind, has_mentions: reviewScope === "referenced" ? "true" : reviewScope === "unreferenced" ? "false" : "", limit: PAGE_SIZE, offset })}` : null);
  const detail = useCuration<RegistryEntityDetail>(entityId ? `${API}entities/${entityId}/` : null);
  const ruleDetail = useCuration<RegistryRuleDetail>(ruleId ? `${API}rules/${ruleId}/` : null);
  const list = view === "review" ? ruleList : entityList;
  useUnsavedChanges(signIn && Boolean(password), "登录信息尚未提交。");

  function urlState(nextView: string, selected = "", search = query, selectedRule = "") {
    const url = new URL(location.href); url.searchParams.set("view", nextView);
    if (selected) url.searchParams.set("entity", selected); else url.searchParams.delete("entity");
    if (selectedRule) url.searchParams.set("rule", selectedRule); else url.searchParams.delete("rule");
    if (search) url.searchParams.set("q", search); else url.searchParams.delete("q");
    history.replaceState(null, "", url);
  }
  function openEntity(id: string) {
    setEntityId(id); setRuleId(""); urlState(view, id);
    requestAnimationFrame(() => document.getElementById("registry-detail")?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }
  function openRule(rule: RegistryRule) {
    setRuleId(rule.id); setEntityId(""); urlState(view, "", query, rule.id);
    requestAnimationFrame(() => document.getElementById("registry-detail")?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }
  function saved(message: string) {
    setNotice(message); detail.refresh(); ruleDetail.refresh(); entityList.refresh(); ruleList.refresh(); summary.refresh();
  }
  async function authenticate(event: FormEvent) {
    event.preventDefault(); setAuthBusy(true); setAuthError("");
    try { await curation("session/", { method: "POST", body: JSON.stringify({ username, password }) }); session.refresh(); setSignIn(false); }
    catch (err) { setAuthError((err as Error).message); } finally { setPassword(""); setAuthBusy(false); }
  }
  async function logout() {
    setAuthBusy(true); setAuthError("");
    try { await curation("session/", { method: "DELETE" }); session.refresh(); }
    catch (err) { setAuthError((err as Error).message); } finally { setAuthBusy(false); }
  }
  function changeView(value: string) {
    setView(value); setOffset(0); setEntityId(""); setRuleId(""); setNotice(""); urlState(value);
  }
  return <AppShell title="实体库与名称对齐" description="独立维护药物、癌种与疾病；确认过的别名和分类按范围复用，原文和限定条件完整保留。" actions={session.data?.username ? <div className="flex items-center gap-2"><span className="hidden text-sm sm:block">{session.data.username}</span><Button variant="outline" size="sm" disabled={authBusy} onClick={logout}><LogOut data-icon="inline-start" />退出</Button></div> : <Button size="sm" variant="outline" onClick={() => setSignIn(true)}><UserRound data-icon="inline-start" />审阅者登录</Button>}>
    <div className="flex min-w-0 flex-col gap-5">
      <Alert><Info /><AlertTitle>本地实体库，确认一次后复用</AlertTitle><AlertDescription>基础条目保留初始快照来源；后续从文献扩展，不在线依赖 tumor_api。主癌种、亚型和分子／部位限定分开保存，目标库的原ID仅作来源对照。</AlertDescription></Alert>
      {notice ? <Alert role="status"><AlertDescription>{notice}</AlertDescription></Alert> : null}
      <ErrorMessage error={authError || session.error} />
      <Tabs value={view} onValueChange={changeView}><TabsList className="w-full sm:w-auto"><TabsTrigger value="drugs">药物</TabsTrigger><TabsTrigger value="diseases">癌种／疾病</TabsTrigger><TabsTrigger value="review">名称与分类审核</TabsTrigger></TabsList></Tabs>
      {summary.data ? <><div className="grid gap-3 sm:grid-cols-3" aria-label="实体库规模"><Card><CardHeader><CardDescription>本地基础与新增实体</CardDescription><CardTitle className="text-2xl tabular-nums">{summary.data.entities.total.toLocaleString()}</CardTitle></CardHeader><CardContent className="text-sm text-muted-foreground">药物 {summary.data.entities.drug} · 癌种／疾病 {summary.data.entities.disease}<br />主癌种 {summary.data.entities.main} · 亚型 {summary.data.entities.subtype} · 层级待定 {summary.data.entities.unassigned}</CardContent></Card><Card><CardHeader><CardDescription>名称规则</CardDescription><CardTitle className="text-2xl tabular-nums">{summary.data.rules.confirmed.toLocaleString()} 已确认</CardTitle></CardHeader><CardContent className="text-sm text-muted-foreground">待审核 {summary.data.rules.proposed} · 待明确 {summary.data.rules.unresolved}<br />字典、规则、模型与人工来源分别保留</CardContent></Card><Card><CardHeader><CardDescription>知识中的登记引用</CardDescription><CardTitle className="text-2xl tabular-nums">{summary.data.mentions.resolved}／{summary.data.mentions.total} 已对齐</CardTitle></CardHeader><CardContent className="text-sm text-muted-foreground">待审核 {summary.data.mentions.proposed} · 待明确 {summary.data.mentions.unresolved} · 非本次范围 {summary.data.mentions.ignored}<br />{summary.data.bootstrap.sealed ? "基础快照已封存，后续使用本地库" : "基础快照尚未封存"}</CardContent></Card></div><JsonDetails label="查看初始化来源与完整统计" value={summary.data} /></> : null}
      {entityId || ruleId ? <section id="registry-detail" className="flex min-w-0 scroll-mt-20 flex-col gap-3" aria-label="当前实体或规则"><div><Button size="sm" variant="outline" onClick={() => { setEntityId(""); setRuleId(""); urlState(view); }}>关闭详情</Button></div>
        {detail.loading || ruleDetail.loading ? <p role="status" className="text-sm text-muted-foreground">正在读取实体与规则…</p> : null}<ErrorMessage error={detail.error || ruleDetail.error} />
        {detail.data ? <EntityDetail entity={detail.data} canReview={Boolean(session.data?.can_review)} onRule={openRule} onEntity={openEntity} onSaved={saved} /> : null}
        {ruleDetail.data ? <RuleDetail rule={ruleDetail.data} canReview={Boolean(session.data?.can_review)} onSaved={saved} onEntity={openEntity} /> : null}
      </section> : null}
      <Card><CardHeader><CardTitle>{view === "drugs" ? "药物实体" : view === "diseases" ? "癌种与疾病实体" : "可复用的名称规则"}</CardTitle><CardDescription>{view === "review" ? "同一名称按适用范围聚合。只审核映射与分类，不改写已提取知识。" : "基础条目与文献新增分别标记；导入存在不代表每一条别名和层级已确认。"}</CardDescription></CardHeader>
        <CardContent className="flex min-w-0 flex-col gap-4"><form onSubmit={(event) => { event.preventDefault(); setQuery(input.trim()); setOffset(0); urlState(view, entityId, input.trim(), ruleId); }}><FieldGroup className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end">
          <Field className="min-w-0 flex-1"><FieldLabel htmlFor="registry-search">搜索名称或别名</FieldLabel><Input id="registry-search" value={input} onChange={(event) => setInput(event.target.value)} placeholder={view === "drugs" ? "例如：dordaviprone、ONC201" : "例如：脑胶质瘤、DMG"} /></Field>
          {view === "diseases" ? <Field className="sm:w-40"><FieldLabel htmlFor="registry-level">层级</FieldLabel><Choice id="registry-level" value={level} onChange={(value) => { setLevel(value); setOffset(0); }} values={[["all", "全部层级"], ["main", "主癌种"], ["subtype", "亚型"], ["unassigned", "层级待定"], ["not_applicable", "非癌种／不适用"]]} /></Field> : null}
          {view === "diseases" ? <Field className="sm:w-44"><FieldLabel htmlFor="registry-blood">来源血液肿瘤标记</FieldLabel><Choice id="registry-blood" value={blood} onChange={(value) => { setBlood(value); setOffset(0); }} values={[["all", "全部标记"], ["true", "血液肿瘤"], ["false", "未标为血液肿瘤"], ["unknown", "未知"]]} /></Field> : null}
          {view !== "review" ? <Field className="sm:w-40"><FieldLabel htmlFor="registry-source">来源</FieldLabel><Choice id="registry-source" value={source} onChange={(value) => { setSource(value); setOffset(0); }} values={[["all", "全部来源"], ["seed", "基础快照"], ["literature_new", "文献新增"]]} /></Field> : <>
            <Field className="sm:w-36"><FieldLabel htmlFor="registry-review-kind">类型</FieldLabel><Choice id="registry-review-kind" value={reviewKind} onChange={(value) => { setReviewKind(value); setOffset(0); }} values={[["all", "全部类型"], ["drug", "药物"], ["disease", "癌种／疾病"]]} /></Field>
            <Field className="sm:w-40"><FieldLabel htmlFor="registry-review-status">规则状态</FieldLabel><Choice id="registry-review-status" value={ruleStatus} onChange={(value) => { setRuleStatus(value); setOffset(0); }} values={[["pending", "全部待处理"], ["proposed", "待审核"], ["unresolved", "待明确"], ["confirmed", "已确认"], ["rejected", "已拒绝"], ["revoked", "已撤销"], ["all", "全部状态"]]} /></Field>
            <Field className="sm:w-44"><FieldLabel htmlFor="registry-review-scope">影响范围</FieldLabel><Choice id="registry-review-scope" value={reviewScope} onChange={(value) => { setReviewScope(value); setOffset(0); }} values={[["referenced", "已影响知识"], ["unreferenced", "未被知识引用"], ["all", "全部规则"]]} /></Field>
          </>}
          <Button type="submit"><Search data-icon="inline-start" />搜索</Button>
        </FieldGroup></form>
          <ErrorMessage error={list.error || summary.error} />
          {list.loading ? <p role="status" className="py-6 text-sm text-muted-foreground">正在读取本地实体库…</p> : null}
          {!list.loading && list.data && !list.data.count ? <NoData title="没有符合条件的记录" description="可调整名称、来源或审核状态；未确定的名称会保留在审核列表。" /> : null}
          {view === "review" && ruleList.data?.results.length ? <Table className="min-w-[580px]"><TableHeader><TableRow><TableHead>原词与对应实体</TableHead><TableHead>状态／复用范围</TableHead><TableHead>引用</TableHead><TableHead>操作</TableHead></TableRow></TableHeader><TableBody>{ruleList.data.results.map((rule) => <TableRow key={rule.id}><TableCell className="max-w-md whitespace-normal break-words"><p>{rule.alias}</p>{rule.provenance?.match_relation === "qualified_form" ? <Badge variant="outline">核心疾病＋条件</Badge> : null}<p className="mt-1 text-xs text-muted-foreground">{rule.canonical_entity?.name || "对应待定"}</p></TableCell><TableCell className="whitespace-normal"><div className="flex flex-wrap gap-1"><StatusBadge value={rule.status} /><StatusBadge value={rule.kind} /></div><p className="mt-1 text-xs text-muted-foreground">{registryLabel(rule.scope.type)} · {registryLabel(rule.origin)}</p></TableCell><TableCell>{rule.mention_count}</TableCell><TableCell><Button size="sm" variant="outline" onClick={() => openRule(rule)}>核对</Button></TableCell></TableRow>)}</TableBody></Table> : null}
          {view !== "review" && entityList.data?.results.length ? <Table className="min-w-[680px]"><TableHeader><TableRow><TableHead>实体名称</TableHead><TableHead>类型／层级</TableHead><TableHead>来源与状态</TableHead><TableHead>引用／名称</TableHead><TableHead>操作</TableHead></TableRow></TableHeader><TableBody>{entityList.data.results.map((entity) => <TableRow key={entity.id}><TableCell className="max-w-sm whitespace-normal break-words"><p>{entity.name}</p>{entity.name_en && entity.name_en !== entity.name ? <p lang="en" className="mt-1 text-xs text-muted-foreground">{entity.name_en}</p> : null}</TableCell><TableCell className="whitespace-normal"><StatusBadge value={entity.level} />{entity.main_cancer ? <p className="mt-1 text-xs text-muted-foreground">主癌种：{entity.main_cancer.name}</p> : null}</TableCell><TableCell className="whitespace-normal"><div className="flex flex-wrap gap-1"><StatusBadge value={entity.source_status} /><StatusBadge value={entity.status} /></div></TableCell><TableCell>{entity.mention_count}／{entity.alias_count}</TableCell><TableCell><Button variant="outline" size="sm" onClick={() => openEntity(entity.id)} aria-label={`查看 ${entity.name} 实体`}>详情</Button></TableCell></TableRow>)}</TableBody></Table> : null}
        </CardContent><CardFooter className="flex flex-wrap items-center justify-between gap-3"><p className="text-sm text-muted-foreground">共 {list.data?.count ?? "—"} 项{list.data?.count ? ` · 当前 ${offset + 1}–${Math.min(offset + PAGE_SIZE, list.data.count)}` : ""}</p><div className="flex gap-2"><Button variant="outline" size="sm" disabled={offset === 0 || list.loading} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}><ChevronLeft data-icon="inline-start" />上一页</Button><Button variant="outline" size="sm" disabled={!list.data || offset + PAGE_SIZE >= list.data.count || list.loading} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页<ChevronRight data-icon="inline-end" /></Button></div></CardFooter>
      </Card>
      <p className="text-xs/relaxed text-muted-foreground">脑胶质瘤作为业务主癌种，弥漫性中线胶质瘤作为亚型；部位和分子限定随原知识保留。所有决定只写入 biomed，不导入 tumor_api。</p>
    </div>
    <Sheet open={signIn} onOpenChange={setSignIn}><SheetContent><SheetHeader><SheetTitle>审阅者登录</SheetTitle><SheetDescription>使用现有biomed审阅账号。登录后可保存实体和分类决定。</SheetDescription></SheetHeader><form onSubmit={authenticate} className="p-4"><FieldGroup><Field><FieldLabel htmlFor="registry-user">账号</FieldLabel><Input id="registry-user" value={username} autoComplete="username" onChange={(event) => setUsername(event.target.value)} required /></Field><Field><FieldLabel htmlFor="registry-password">密码</FieldLabel><Input id="registry-password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></Field><ErrorMessage error={authError} /><Button disabled={authBusy}>{authBusy ? "正在登录…" : "登录"}</Button></FieldGroup></form></SheetContent></Sheet>
  </AppShell>;
}
