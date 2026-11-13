import { useEffect, useState, type FormEvent } from "react";
import { ArrowUpRight, Info, Search } from "lucide-react";
import { AppShell } from "@/layouts/AppShell";
import { ErrorMessage, NoData } from "@/components/curation/Common";
import { ArticleSource, FactFields, JsonDetails, KnowledgeStatusBadges, ResultFields, knowledgeDisplayLabel } from "@/components/knowledge/KnowledgeRecordSources";
import { EvidenceContext, EvidenceLabels } from "@/components/knowledge/EvidencePresentation";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { curation, date, STATES, useCuration, type Session } from "@/lib/curation";
import { knowledgeLabel, knowledgeQuery, type KnowledgePage } from "@/lib/knowledge-candidates";
import { KNOWLEDGE_EXAMPLE, KNOWLEDGE_FILTERS, type KnowledgeFilters, type KnowledgeReview, type KnowledgeSupport, type PublishedKnowledge } from "@/lib/knowledge-search";
import { useUnsavedChanges } from "@/lib/navigation";

const PAGE_SIZE = 20;

function SupportingQuote({ source }: { source: KnowledgeSupport }) {
  const [open, setOpen] = useState(false);
  return <figure className="flex min-w-0 flex-col gap-2">
    <blockquote lang="en" className="whitespace-pre-wrap break-words border-l-2 pl-3 text-sm/relaxed">{source.quote}</blockquote>
    <figcaption className="flex flex-col gap-1 break-all text-xs text-muted-foreground">
      <span>{source.passage?.locator} · 字符 [{source.start}, {source.end}) · {source.passage?.parser_version}</span>
      {source.fields?.length ? <span>核验支持字段：{source.fields.map(knowledgeDisplayLabel).join("、")}</span> : null}
      <span>段落 ID：{source.passage_id}</span>
    </figcaption>
    {source.passage ? <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="cursor-pointer text-xs">查看完整英文段落与版本</summary>
      {open ? <div className="mt-2 flex min-w-0 flex-col gap-2">
        <p lang="en" className="whitespace-pre-wrap break-words text-sm/relaxed">{source.passage.text}</p>
        <p className="break-all text-xs text-muted-foreground">文章版本：{source.passage.version_id} · 段落 SHA-256：{source.passage.text_sha256}</p>
      </div> : null}
    </details> : null}
  </figure>;
}

function MatchReasons({ item }: { item: PublishedKnowledge }) {
  if (!item.matched_terms.length) return null;
  return <section aria-label={`${item.group_id} 的检索匹配依据`} className="flex flex-col gap-2">
    <h3 className="text-sm font-medium">匹配依据</h3>
    <ul className="flex flex-col gap-1 text-sm">{item.matched_terms.map((match, index) => <li key={`${match.role}:${match.term}:${index}`}>
      <Badge variant="outline">{knowledgeLabel(match.role)}</Badge>{" "}<span>{match.term}</span>
      <span className="ml-2 text-xs text-muted-foreground">{knowledgeDisplayLabel(match.basis)}</span>
    </li>)}</ul>
  </section>;
}

function ReviewHistory({ reviews }: { reviews: KnowledgeReview[] }) {
  return <section className="flex flex-col gap-3" aria-label="知识审核历史">
    <h3 className="font-medium">审核历史</h3>
    {reviews.length ? reviews.map((review) => <article key={review.id} className="flex min-w-0 flex-col gap-2 rounded-lg border p-3">
      <p className="text-sm">{review.kind === "ai" ? "AI 核验" : `人工抽查 · ${review.actor ?? "未记录账号"}`} · {STATES[review.decision] ?? review.decision} · {date(review.created_at)}</p>
      <p className="whitespace-pre-wrap break-words text-sm/relaxed">{review.reason}</p>
      {review.kind === "ai" ? <p className="text-xs text-muted-foreground">{review.provider} · {review.model} · 审核规则 {review.policy_version}</p> : null}
      <JsonDetails title="查看审核版本与记录" value={review} />
    </article>) : <p className="text-sm text-muted-foreground">暂无审核记录。</p>}
  </section>;
}

function HumanReview({ item, onReviewed, onDirty }: { item: PublishedKnowledge; onReviewed: () => void; onDirty: (value: boolean) => void }) {
  const session = useCuration<Session>("session/");
  const [signIn, setSignIn] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const dirty = Boolean(reason.trim() || password);
  useUnsavedChanges(dirty, "人工抽查还有未提交的说明或登录输入。");
  useEffect(() => { onDirty(dirty); return () => onDirty(false); }, [dirty, onDirty]);

  async function authenticate(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await curation("session/", { method: "POST", body: JSON.stringify({ username, password }) });
      session.refresh(); setSignIn(false);
    } catch (err) { setError((err as Error).message); }
    finally { setPassword(""); setBusy(false); }
  }

  async function signOut() {
    if (reason.trim() && !window.confirm("抽查说明尚未提交，退出会清除说明。确定退出吗？")) return;
    setBusy(true); setError("");
    try { await curation("session/", { method: "DELETE" }); setReason(""); session.refresh(); }
    catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }

  async function review(decision: "confirm" | "flag") {
    setBusy(true); setError(""); setNotice("");
    try {
      await curation(`knowledge/${encodeURIComponent(item.id)}/review/`, { method: "POST", body: JSON.stringify({ decision, reason: reason.trim() }) });
      setReason(""); onDirty(false);
      setNotice(decision === "flag" ? "已保存问题说明并撤回知识，原文与审核历史保留。" : "人工抽查确认已保存。");
      onReviewed();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }

  return <section className="flex flex-col gap-3" aria-label="人工抽查">
    <h3 className="font-medium">人工抽查</h3>
    <p className="text-xs text-muted-foreground">抽查与 AI 核验独立；记录当前账号和说明。发现问题后撤回，原文与历史保留。</p>
    <ErrorMessage error={error || session.error} />
    {notice ? <p role="status" className="text-sm">{notice}</p> : null}
    {session.loading ? <Skeleton className="h-10 w-full" /> : session.data?.username ? <div className="flex flex-wrap items-center gap-3 text-sm"><span>当前账号：{session.data.username}</span><Button size="sm" variant="outline" disabled={busy} onClick={signOut}>退出</Button></div> : <div><Button variant="outline" size="sm" onClick={() => setSignIn(!signIn)} aria-expanded={signIn}>审阅者登录</Button></div>}
    {signIn && !session.data?.username ? <form onSubmit={authenticate}>
      <FieldGroup className="max-w-md">
        <Field><FieldLabel htmlFor="knowledge-review-username">账号</FieldLabel><Input id="knowledge-review-username" autoComplete="username" required value={username} onChange={(event) => setUsername(event.target.value)} /></Field>
        <Field><FieldLabel htmlFor="knowledge-review-password">密码</FieldLabel><Input id="knowledge-review-password" type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} /></Field>
        <div><Button type="submit" disabled={busy || !username || !password}>登录</Button></div>
      </FieldGroup>
    </form> : null}
    {session.data?.can_review ? <FieldGroup>
      <Field><FieldLabel htmlFor="knowledge-review-reason">核验结论或问题说明（必填）</FieldLabel><Textarea id="knowledge-review-reason" value={reason} onChange={(event) => setReason(event.target.value)} maxLength={10000} disabled={busy} /></Field>
      <div className="flex flex-wrap gap-2">
        <Button disabled={busy || !reason.trim()} onClick={() => review("confirm")}>确认原文与知识一致</Button>
        <Button variant="destructive" disabled={busy || !reason.trim()} onClick={() => review("flag")}>发现问题并撤回</Button>
      </div>
    </FieldGroup> : session.data?.username ? <p className="text-sm text-muted-foreground">当前账号没有知识抽查权限。</p> : null}
  </section>;
}

function KnowledgeDetail({ item, onReviewed, onDirty }: { item: PublishedKnowledge; onReviewed: () => void; onDirty: (value: boolean) => void }) {
  return <Card className="min-w-0" aria-label={`${item.group_id} 知识详情`}>
    <CardHeader>
      <div className="flex flex-wrap items-center justify-between gap-2"><CardTitle>{item.group_id} · 知识详情</CardTitle><KnowledgeStatusBadges item={item} /></div>
      <CardDescription>结构化字段与英文证据分开显示，可追溯至文章版本和具体段落。</CardDescription>
    </CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-6">
      {item.publication_status !== "published" ? <Alert variant="destructive"><AlertTitle>当前未发布</AlertTitle><AlertDescription>此页保留历史记录；当前知识不进入已发布检索结果。</AlertDescription></Alert> : null}
      <ArticleSource article={item.article} />
      <EvidenceContext data={item.evidence_presentation} />
      <section className="flex flex-col gap-3" aria-label="原始结构化知识">
        <h3 className="font-medium">原始抽取字段</h3><FactFields fact={item.fact} />
        <ResultFields results={item.fact.results} />
        <JsonDetails title="查看完整原始关系结构" value={item.fact} />
      </section>
      <MatchReasons item={item} />
      <section className="flex flex-col gap-3" aria-label="核验通过的主关系实体">
        <h3 className="font-medium">核验通过的主关系实体</h3>
        <p className="text-xs text-muted-foreground">检索只匹配这些角色中的已核验词条及别名；文章中出现的其他词不自动加入主关系。</p>
        {item.approved_links.map((link) => <article key={`${link.entity_id}:${link.role}`} className="flex min-w-0 flex-col gap-2 rounded-lg border p-3">
          <p className="text-sm"><Badge variant="outline">{knowledgeLabel(link.role)}</Badge>{" "}<span lang="en">{link.label}</span></p>
          {link.context ? <p className="text-sm text-muted-foreground">关联角色：{link.context}</p> : null}
          {link.aliases.length ? <ul className="flex flex-col gap-1 text-xs text-muted-foreground">{link.aliases.map((alias, index) => <li key={`${alias.term}:${index}`}>{alias.term} · {knowledgeDisplayLabel(alias.basis)}</li>)}</ul> : null}
          <Button size="sm" variant="outline" className="self-start" asChild><a href={`/knowledge/candidates/?entity=${encodeURIComponent(link.entity_id)}`}>查看词条来源 <ArrowUpRight data-icon="inline-end" /></a></Button>
          <JsonDetails title="查看该关联的原文支持位置" value={link.support} />
        </article>)}
      </section>
      <section className="flex min-w-0 flex-col gap-4" aria-label="核验通过的英文原文">
        <h3 className="font-medium">核验通过的英文引用（{item.supporting_quotes.length} 处）</h3>
        {item.supporting_quotes.map((source, index) => <SupportingQuote key={`${source.passage_id}:${source.start}:${source.end}:${index}`} source={source} />)}
      </section>
      <ReviewHistory reviews={item.review_history ?? (item.review ? [item.review] : [])} />
      <HumanReview item={item} onReviewed={onReviewed} onDirty={onDirty} />
    </CardContent>
    <CardFooter className="flex flex-col items-start gap-2">
      <Button size="sm" variant="outline" asChild><a href={`/knowledge/candidates/?candidate=${encodeURIComponent(item.id)}`}>查看原始候选 <ArrowUpRight data-icon="inline-end" /></a></Button>
      <p className="break-all text-xs text-muted-foreground">知识 ID：{item.id} · {item.published_at ? `发布时间：${date(item.published_at)}` : "尚未发布"}</p>
    </CardFooter>
  </Card>;
}

export default function KnowledgeSearchApp() {
  const [input, setInput] = useState<KnowledgeFilters>(() => {
    const params = new URLSearchParams(location.search);
    return { disease: params.get("disease") ?? "", drug: params.get("drug") ?? "", biomarker: params.get("biomarker") ?? "", treatment: params.get("treatment") ?? "" };
  });
  const [filters, setFilters] = useState(input);
  const [selected, setSelected] = useState(() => new URLSearchParams(location.search).get("knowledge") ?? "");
  const [offset, setOffset] = useState(0);
  const [reviewDirty, setReviewDirty] = useState(false);
  const list = useCuration<KnowledgePage<PublishedKnowledge>>(`knowledge/?${knowledgeQuery({ ...filters, limit: PAGE_SIZE, offset })}`);
  const detail = useCuration<PublishedKnowledge>(selected ? `knowledge/${encodeURIComponent(selected)}/` : null);

  function selectDetail(id: string) {
    if (reviewDirty && !window.confirm("人工抽查说明尚未提交，切换详情会清除输入。确定继续吗？")) return;
    setSelected(id); setReviewDirty(false);
    const url = new URL(location.href);
    if (id) url.searchParams.set("knowledge", id); else url.searchParams.delete("knowledge");
    history.replaceState(null, "", url);
    if (id) requestAnimationFrame(() => document.getElementById("published-knowledge-detail")?.scrollIntoView({ block: "start", behavior: "smooth" }));
  }

  function search(values: KnowledgeFilters) {
    if (reviewDirty && !window.confirm("人工抽查说明尚未提交，重新检索会清除输入。确定继续吗？")) return;
    const next = Object.fromEntries(Object.entries(values).map(([key, value]) => [key, value.trim()])) as KnowledgeFilters;
    setInput(next); setFilters(next); setOffset(0); setSelected(""); setReviewDirty(false);
    const url = new URL(location.href);
    url.searchParams.delete("knowledge");
    for (const { key } of KNOWLEDGE_FILTERS) { if (next[key]) url.searchParams.set(key, next[key]); else url.searchParams.delete(key); }
    history.replaceState(null, "", url);
  }

  return <AppShell title="知识检索" description="从已核验并发布的知识中，组合查询癌种、具体药物与生物标志物，回到原始英文证据。">
    <div className="flex min-w-0 flex-col gap-5">
      <Alert><Info /><AlertTitle>同一条知识同时满足所填条件</AlertTitle><AlertDescription>只匹配已核验的主关系参与实体。癌种和药物支持原词及已确认的标准名称、别名和主癌种；背景提及或对照不算实际干预。AI 通过即可发布，人工抽查状态独立。</AlertDescription></Alert>
      <Card>
        <CardHeader><CardTitle>组合检索</CardTitle><CardDescription>仅显示已发布记录；多个条件为 AND，留空表示不限定。</CardDescription></CardHeader>
        <CardContent>
          <form onSubmit={(event) => { event.preventDefault(); search(input); }}>
            <FieldGroup className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">{KNOWLEDGE_FILTERS.map(({ key, label, example }) => <Field key={key}>
              <FieldLabel htmlFor={`knowledge-filter-${key}`}>{label}</FieldLabel>
              <Input id={`knowledge-filter-${key}`} value={input[key]} onChange={(event) => setInput((value) => ({ ...value, [key]: event.target.value }))} placeholder={example} />
            </Field>)}</FieldGroup>
            <div className="mt-4 flex flex-wrap gap-2"><Button type="submit"><Search data-icon="inline-start" />检索知识</Button><Button type="button" variant="outline" onClick={() => search(KNOWLEDGE_EXAMPLE)}>示例：胆道癌＋durvalumab＋CRP</Button></div>
          </form>
        </CardContent>
      </Card>
      {selected ? <section id="published-knowledge-detail" className="flex min-w-0 scroll-mt-20 flex-col gap-3">
        <div><Button variant="outline" size="sm" onClick={() => selectDetail("")}>关闭知识详情</Button></div>
        <ErrorMessage error={detail.error} />
        {detail.loading ? <Skeleton className="h-48 w-full" /> : detail.data ? <KnowledgeDetail key={detail.data.id} item={detail.data} onDirty={setReviewDirty} onReviewed={() => { list.refresh(); detail.refresh(); }} /> : null}
      </section> : null}
      <section className="flex min-w-0 flex-col gap-4" aria-label="已发布知识检索结果">
        <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-medium">检索结果{list.data ? `（${list.data.count} 条）` : ""}</h2><Button variant="outline" size="sm" asChild><a href="/knowledge/candidates/">查看全部研究候选 <ArrowUpRight data-icon="inline-end" /></a></Button></div>
        <ErrorMessage error={list.error} />
        {list.loading ? <Skeleton className="h-36 w-full" /> : list.data?.results.length ? list.data.results.map((item) => <Card key={item.id} className="min-w-0">
          <CardHeader><div className="flex flex-wrap items-center justify-between gap-2"><CardTitle>{item.group_id}</CardTitle><KnowledgeStatusBadges item={item} /></div><CardDescription>{item.article.title} · PMID {item.article.pmid}</CardDescription></CardHeader>
          <CardContent className="flex min-w-0 flex-col gap-4"><EvidenceLabels data={item.evidence_presentation} /><MatchReasons item={item} /><FactFields fact={item.fact} /><ResultFields results={item.fact.results} /></CardContent>
          <CardFooter><Button variant="outline" size="sm" onClick={() => selectDetail(item.id)} aria-label={`查看 ${item.group_id} 已发布知识与英文原文`}>查看知识与英文原文</Button></CardFooter>
        </Card>) : list.data ? <NoData title="没有同时满足条件的已发布知识" description="可调整词条或减少条件。全文中出现某个词，不代表它参与本条知识的主关系。" /> : null}
        {list.data ? <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-xs text-muted-foreground">共 {list.data.count} 条{list.data.count ? ` · 当前 ${offset + 1}–${Math.min(offset + PAGE_SIZE, list.data.count)}` : ""}</p><div className="flex gap-2"><Button size="sm" variant="outline" disabled={!offset || list.loading} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</Button><Button size="sm" variant="outline" disabled={offset + PAGE_SIZE >= list.data.count || list.loading} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</Button></div></div> : null}
      </section>
    </div>
  </AppShell>;
}
