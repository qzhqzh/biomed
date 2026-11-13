import { useState } from "react";
import { ArrowUpRight, FlaskConical, Info } from "lucide-react";
import { AppShell } from "@/layouts/AppShell";
import { ErrorMessage, NoData } from "@/components/curation/Common";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TargetCompatibilityPanel } from "@/components/knowledge/TargetCompatibilityPanel";
import { EntityResolutionCases } from "@/components/knowledge/EntityResolutionCases";
import { useCuration } from "@/lib/curation";
import {
  cohortLabel, researchKnowledgeQualityArms, researchLabel, researchNormalizationLabel,
  type ResearchCase, type ResearchExperiment, type ResearchIndex,
  type ResearchKnowledgeCandidate, type ResearchLabel, type ResearchMetric, type ResearchQuote,
  type ModelComparison,
} from "@/lib/research";

function TextList({ items }: { items: string[] }) {
  return <ul className="flex list-disc flex-col gap-2 pl-5">
    {items.map((item, index) => <li key={index}>{item}</li>)}
  </ul>;
}

function Metric({ metric }: { metric: ResearchMetric }) {
  const hasRatio = metric.numerator !== undefined && metric.denominator !== undefined;
  const ratio = hasRatio && metric.denominator! > 0
    ? Math.max(0, Math.min(100, metric.numerator! / metric.denominator! * 100)) : null;
  return <div className="flex flex-col gap-1.5 py-2">
    <div className="flex flex-wrap justify-between gap-2">
      <span>{metric.label}</span>
      <strong className="tabular-nums">
        {hasRatio ? `${metric.numerator} / ${metric.denominator}` : metric.value ?? "未测量"}
        {hasRatio && ratio !== null ? `（${ratio.toFixed(1)}%）` : metric.unit ? ` ${metric.unit}` : ""}
      </strong>
    </div>
    {ratio !== null ? <div className="h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden="true">
      <div className="h-full rounded-full bg-primary" style={{ width: `${ratio}%` }} />
    </div> : null}
    <p className="text-xs/relaxed text-muted-foreground">{metric.note}</p>
  </div>;
}

function Quotes({ quotes }: { quotes: ResearchQuote[] }) {
  return <div className="flex flex-col gap-3">
    {quotes.map((quote, index) => <figure key={index} className="min-w-0">
      <blockquote className="break-words border-l-2 pl-3 text-sm/relaxed">{quote.text}</blockquote>
      <figcaption className="mt-1 text-xs text-muted-foreground">
        {researchLabel(quote.field)} · 字符 [{quote.start}, {quote.end})
      </figcaption>
    </figure>)}
  </div>;
}

function Labels({ labels }: { labels: ResearchLabel[] }) {
  if (!labels.length) return <p className="text-muted-foreground">未返回多轴标签。</p>;
  return <div className="flex flex-col gap-2">
    {labels.map((label, index) => <details key={`${label.axis}-${label.id}-${index}`}>
      <summary className="cursor-pointer text-xs/relaxed">
        {researchLabel(label.axis)}：{label.label}
        {label.role ? ` · ${researchLabel(label.role)}` : ""}
      </summary>
      <div className="mt-2"><Quotes quotes={label.matches} /></div>
    </details>)}
  </div>;
}

function CaseDetail({ row, threeClass }: { row: ResearchCase; threeClass: boolean }) {
  const valid = row.model_status === "validated_candidate";
  return <section className="flex min-w-0 flex-col gap-4" aria-label="逐篇文献详情">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 flex-1">
        <h3 className="text-base font-semibold">{row.title}</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          {cohortLabel(row.cohort)} · 第 {row.rank} 篇 · PMID {row.pmid} · {researchLabel(row.source_scope)}
        </p>
      </div>
      <Button variant="outline" size="sm" asChild>
        <a href={`https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(row.pmid)}/`} target="_blank" rel="noreferrer">
          PubMed 记录 <ArrowUpRight data-icon="inline-end" />
        </a>
      </Button>
    </div>
    <div className="grid min-w-0 gap-4 xl:grid-cols-3">
      <Card>
        <CardHeader>
          <CardTitle>Astra 参考</CardTitle>
          <CardDescription>{researchLabel(row.reference_state)} · 模型参考，可修订</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p>主类：<strong>{researchLabel(row.reference_relevance)}</strong></p>
          {!threeClass ? <p>分流：{researchLabel(row.reference_scope)}</p> : null}
          <p className="text-muted-foreground">{row.reference_notes}</p>
          <Quotes quotes={row.reference_quotes} />
          {!threeClass ? <details>
            <summary className="cursor-pointer">参考多轴标签（{row.reference_labels.length}）</summary>
            <div className="mt-3"><Labels labels={row.reference_labels} /></div>
          </details> : null}
        </CardContent>
        <CardFooter><Badge variant="outline">{row.scope_eligible ? "参考可判定，计入对照" : "参考未决，单列保留"}</Badge></CardFooter>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>规则基线</CardTitle>
          <CardDescription>{threeClass ? "关键词／正则匹配，保守映射到三分类；没有命中不能当作非肿瘤。" : "规则只提供候选分流；没有九类主相关性预测。"}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p>分类：<strong>{row.rule_scope ? researchLabel(row.rule_scope) : "未完成分类"}</strong></p>
          <p className="text-xs text-muted-foreground">展开规则可看实际命中文字、来源字段与字符位置。</p>
          <Labels labels={threeClass ? row.rule_labels.filter((label) => label.axis === "boundary") : row.rule_labels} />
          {threeClass ? <details><summary className="cursor-pointer">其他关键词命中（不直接决定三分类）</summary><Labels labels={row.rule_labels.filter((label) => label.axis !== "boundary")} /></details> : null}
        </CardContent>
        <CardFooter><Badge variant="secondary">{researchLabel(row.rule_outcome)}</Badge></CardFooter>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Qwen3-1.7B</CardTitle>
          <CardDescription>{researchLabel(row.model_status)}{row.model_seconds !== null ? ` · ${row.model_seconds} 秒` : ""}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p>合格主类：<strong>{valid ? researchLabel(row.model_relevance) : "无合格输出"}</strong></p>
          {!threeClass ? <p>合格分流：{valid ? researchLabel(row.model_scope) : "无合格输出"}</p> : null}
          {!valid && row.model_raw_relevance ? <Alert>
            <Info />
            <AlertDescription>原始主类：{researchLabel(row.model_raw_relevance)}（整份校验失败，仅用于诊断，未计作有效预测）。</AlertDescription>
          </Alert> : null}
          {row.model_error ? <p className="break-words text-xs/relaxed text-destructive">校验问题：{row.model_error}</p> : null}
          {threeClass ? <p className="text-xs text-muted-foreground">本轮只要求一个分类，不要求部位、理由或引用。</p> : <Labels labels={row.model_labels} />}
        </CardContent>
        <CardFooter className="flex flex-wrap gap-2">
          <Badge variant="secondary">主类：{researchLabel(row.model_relevance_outcome)}</Badge>
          {!threeClass ? <Badge variant="outline">分流：{researchLabel(row.model_outcome)}</Badge> : null}
        </CardFooter>
      </Card>
    </div>
  </section>;
}

function Cases({ records, threeClass }: { records: ResearchCase[]; threeClass: boolean }) {
  const [cohort, setCohort] = useState("all");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState("");
  const cohorts = [...new Set(records.map((row) => row.cohort))];
  const query = search.trim().toLowerCase();
  const filtered = records.filter((row) => (cohort === "all" || row.cohort === cohort)
    && (!query || `${row.title} ${row.pmid}`.toLowerCase().includes(query)));
  const active = filtered.find((row) => row.pmid === selected) ?? filtered[0];
  return <div className="flex min-w-0 flex-col gap-5">
    <FieldGroup className="md:flex-row">
      {cohorts.length > 1 ? <Field className="md:max-w-xs">
        <FieldLabel htmlFor="research-cohort">样本组</FieldLabel>
        <Select value={cohort} onValueChange={setCohort}>
          <SelectTrigger id="research-cohort"><SelectValue /></SelectTrigger>
          <SelectContent><SelectGroup>
            <SelectItem value="all">全部样本（{records.length} 篇）</SelectItem>
            {cohorts.map((item) => <SelectItem value={item} key={item}>{cohortLabel(item)}</SelectItem>)}
          </SelectGroup></SelectContent>
        </Select>
      </Field> : null}
      <Field>
        <FieldLabel htmlFor="research-case-search">查找文献</FieldLabel>
        <Input id="research-case-search" placeholder="标题或 PMID" value={search} onChange={(event) => setSearch(event.target.value)} />
      </Field>
    </FieldGroup>
    <p className="text-xs text-muted-foreground">显示 {filtered.length} / {records.length} 篇。点击文献查看原文依据；筛选不会重新计算整项实验的指标。</p>
    {filtered.length ? <>
      <div className="max-h-96 overflow-auto rounded-lg border">
        <Table>
          <TableHeader><TableRow>
            <TableHead>文献</TableHead><TableHead>Astra 主类</TableHead>
            <TableHead>规则分类</TableHead><TableHead>小模型主类</TableHead><TableHead>输出状态</TableHead>
          </TableRow></TableHeader>
          <TableBody>{filtered.map((row) => <TableRow key={row.pmid} data-state={active?.pmid === row.pmid ? "selected" : undefined}>
            <TableCell className="max-w-sm whitespace-normal">
              <button type="button" className="text-left text-sm hover:underline focus-visible:outline-2 focus-visible:outline-ring" onClick={() => setSelected(row.pmid)} aria-pressed={active?.pmid === row.pmid}>
                {row.title}<span className="mt-1 block text-xs text-muted-foreground">PMID {row.pmid} · {cohortLabel(row.cohort)}</span>
              </button>
            </TableCell>
            <TableCell>{researchLabel(row.reference_relevance)}</TableCell>
            <TableCell>{row.rule_scope ? researchLabel(row.rule_scope) : "未完成"}</TableCell>
            <TableCell>{row.model_status === "validated_candidate" ? researchLabel(row.model_relevance) : "无合格输出"}</TableCell>
            <TableCell><Badge variant={row.model_status === "invalid_output" ? "destructive" : "secondary"}>{researchLabel(row.model_status)}</Badge></TableCell>
          </TableRow>)}</TableBody>
        </Table>
      </div>
      {active ? <CaseDetail key={active.pmid} row={active} threeClass={threeClass} /> : null}
    </> : <NoData title="没有匹配的文献" description="调整样本组或查找条件，原始实验记录保持不变。" />}
  </div>;
}

function KnowledgeCandidate({ row }: { row: ResearchKnowledgeCandidate }) {
  const [expanded, setExpanded] = useState(false);
  const qualityArms = researchKnowledgeQualityArms(row.mapping.preview);
  // The displayed entities belong to arm A; task coverage does not invalidate its extra facts.
  const quality = qualityArms.find((arm) => /^A(?:\s|·)/.test(arm.label))?.quality;
  const status = quality
    ? !quality.core_valid ? "A · 核心知识需修正" : !quality.participants_usable ? "A · 参与实体需修正" : "A · 知识有效"
    : row.status === "abstained" ? "未形成候选" : researchLabel(row.status);
  const invalid = quality ? !quality.core_valid || !quality.participants_usable
    : row.status === "unsupported" || row.status === "rejected" || row.status === "invalid_output";
  const scope = row.study_scope === "mixed" ? "混合研究层级" : researchLabel(row.study_scope);
  const detailId = `knowledge-candidate-${row.id}`;
  return <Card className="min-w-0">
    <CardHeader>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <CardTitle>{row.id} · 知识候选</CardTitle>
        <div className="flex flex-wrap gap-2">
          <Badge variant={invalid ? "destructive" : "secondary"}>{status}</Badge>
          {quality ? <Badge variant="outline">A · {quality.target_covered ? "目标已覆盖" : "目标覆盖未完成"}</Badge> : null}
        </div>
      </div>
      <CardDescription>{scope} · {researchLabel(row.source_role)}</CardDescription>
    </CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-4">
      <p className="break-words text-sm/relaxed">{row.statement ?? "本次没有形成可展示的知识陈述。"}</p>
      <div className="min-w-0">
        <p className="text-xs font-medium text-muted-foreground">文章出处</p>
        <a className="mt-1 block break-words text-sm hover:underline" href={`https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(row.pmid)}/`} target="_blank" rel="noreferrer">
          {row.title} · PMID {row.pmid} ↗
        </a>
      </div>
      <p className="text-sm/relaxed"><strong>独立核验：</strong>{row.review_note || "尚未提供核验说明。"}</p>
      {qualityArms.length ? <section aria-label={`${row.id} 的独立质量`} className="flex flex-col gap-2 rounded-lg border p-3 text-xs/relaxed">
        {qualityArms.map((arm) => <p key={arm.label}>
          <strong>{arm.label}</strong><br />
          核心知识有效：{arm.quality.core_valid ? "是" : "否"} · 关系参与实体可用：{arm.quality.participants_usable ? "是" : "否"} · 目标已覆盖：{arm.quality.target_covered ? "是" : "否"}
        </p>)}
        <p className="text-muted-foreground">知识有效性看核心知识与关系参与实体；目标覆盖单独记录。三项同时满足计为实验任务完成，未覆盖目标不等于已提取知识无效。</p>
      </section> : null}
      {expanded ? <div id={detailId} className="flex min-w-0 flex-col gap-5">
        <section aria-label={`${row.id} 的实体`} className="flex min-w-0 flex-col gap-2">
          <h3 className="font-medium">实体与标准化</h3>
          {row.entities.length ? <Table>
            <TableHeader><TableRow><TableHead>实体</TableHead><TableHead>类型</TableHead><TableHead>标准 ID</TableHead><TableHead>匹配状态</TableHead></TableRow></TableHeader>
            <TableBody>{row.entities.map((entity) => <TableRow key={entity.id}>
              <TableCell className="whitespace-normal break-words">{entity.text}<span className="block text-xs text-muted-foreground">{entity.id}</span></TableCell>
              <TableCell>{researchLabel(entity.type)}</TableCell>
              <TableCell className="whitespace-normal break-all">{entity.standard_ids.length ? entity.standard_ids.join("、") : "未提供标准 ID"}</TableCell>
              <TableCell><Badge variant={entity.normalization_status === "unanchored" || entity.normalization_status === "invalid_entity" ? "destructive" : "outline"}>{researchNormalizationLabel(entity.normalization_status)}</Badge></TableCell>
            </TableRow>)}</TableBody>
          </Table> : <p className="text-sm text-muted-foreground">没有合格实体输出。</p>}
        </section>
        <section aria-label={`${row.id} 的片段出处`} className="flex min-w-0 flex-col gap-3">
          <h3 className="font-medium">片段出处与字段支持</h3>
          {row.quotes.length ? row.quotes.map((quote, index) => <figure key={`${quote.passage_id}-${index}`} className="min-w-0">
            <blockquote className="break-words border-l-2 pl-3 text-sm/relaxed">{quote.quote}</blockquote>
            <figcaption className="mt-1 flex flex-col gap-1 text-xs/relaxed text-muted-foreground">
              <span className="break-all">{quote.locator} · {quote.start !== null && quote.end !== null ? `字符 [${quote.start}, ${quote.end})` : "字符位置未确定"}</span>
              <span>模型标注的支持字段：{quote.fields.map(researchLabel).join("、") || "未标记"}（精确定位不等于语义核验通过）</span>
              <span className="break-all">片段 ID：{quote.passage_id}</span>
            </figcaption>
          </figure>) : <p className="text-sm text-muted-foreground">没有通过校验的原文引用。</p>}
        </section>
        <section aria-label={`${row.id} 的结构映射`} className="flex min-w-0 flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2"><h3 className="font-medium">{row.mapping.target === "biomed" ? "biomed 入库结构与溯源" : "tumor_api 证据中心映射预览"}</h3><Badge variant="outline">{researchLabel(row.mapping.status)}</Badge></div>
          <p className="text-xs text-muted-foreground">{row.mapping.target === "biomed" ? row.published_knowledge_id ? "本条知识已按本轮核验结果发布至 biomed；可查看实际发布记录及原文出处。" : "研究候选保存在 biomed；原文出处与关联依据独立记录，实际发布状态见入库记录。" : "仅展示结构对应关系；未向 tumor_api 写入数据。标准实体 ID 不等于目标库的内部 ID。"}</p>
          {row.mapping.blockers.length ? <TextList items={row.mapping.blockers} /> : null}
          <details className="min-w-0 rounded-lg border p-3">
            <summary className="cursor-pointer text-sm">展开映射字段</summary>
            <pre className="mt-3 overflow-x-auto whitespace-pre-wrap break-all text-xs/relaxed">{JSON.stringify(row.mapping.preview, null, 2)}</pre>
          </details>
        </section>
      </div> : null}
    </CardContent>
    <CardFooter className="flex flex-wrap gap-2">
      <Button variant="outline" size="sm" onClick={() => setExpanded(!expanded)} aria-expanded={expanded} aria-controls={expanded ? detailId : undefined}>
        {expanded ? "收起实体、原文和映射" : "查看实体、原文和映射"}
      </Button>
      {row.published_knowledge_id ? <Button variant="outline" size="sm" asChild>
        <a href={`/knowledge/search/?knowledge=${encodeURIComponent(row.published_knowledge_id)}`}>查看已发布知识 <ArrowUpRight data-icon="inline-end" /></a>
      </Button> : null}
      {row.stored_candidate_id ? <Button variant="outline" size="sm" asChild>
        <a href={`/knowledge/candidates/?candidate=${encodeURIComponent(row.stored_candidate_id)}`}>查看实际入库记录 <ArrowUpRight data-icon="inline-end" /></a>
      </Button> : null}
    </CardFooter>
  </Card>;
}

function KnowledgeCandidates({ records }: { records: ResearchKnowledgeCandidate[] }) {
  return <div className="flex min-w-0 flex-col gap-4">
    <Alert>
      <Info />
      <AlertTitle>{records.length} 条抽取记录 · 文章与片段两层出处</AlertTitle>
      <AlertDescription>核验来源与口径见本次方法及版本说明。逐条展开可核对原始字段、实体、原文支持字段与映射限制；已入库条目提供实际记录入口，发布状态单独保留。</AlertDescription>
    </Alert>
    {records.map((row) => <KnowledgeCandidate key={row.id} row={row} />)}
  </div>;
}

function RuleExplanation({ data }: { data: NonNullable<ResearchExperiment["rules_explanation"]> }) {
  const examples: Record<string, string> = {
    solid_malignancy: "匹配 carcinoma、glioma、lung cancer 等肿瘤类型或部位＋癌的表达。",
    solid_cell_model: "匹配 HCT116、JIMT1，另要求标题或摘要中出现 cancer / tumor / neoplasm。",
    hematologic: "匹配 leukemia、lymphoma、myeloma 等血液肿瘤表达。",
    hematologic_abbreviation: "仅在标题中匹配大小写严格的 AML、CLL、DLBCL 等缩写。",
    benign: "匹配 benign、adenoma 等良性线索；本轮不单凭这些词确认三分类。",
    precursor: "匹配 polyp、dysplasia 等癌前/息肉线索；本轮不单凭这些词确认三分类。",
  };
  return <Card>
    <CardHeader><CardTitle>规则 v3 到底匹配了什么？</CardTitle><CardDescription>固定词表＋正则表达式，不是小模型。共 {data.total_rules} 条规则，其中下面 6 条决定原始范围。</CardDescription></CardHeader>
    <CardContent className="flex flex-col gap-4">
      <p>在标题、摘要、MeSH 和出版类型中找词；部位词本身不代表肿瘤。下面可展开查看实际运行的完整表达式与排除条件。</p>
      <div className="flex flex-col gap-3">{data.main_scope_rules.map((rule) => <details key={rule.id} className="rounded-lg border p-3">
        <summary className="cursor-pointer font-medium">{rule.label} <span className="text-xs font-normal text-muted-foreground">{rule.id}</span></summary>
        <div className="mt-3 flex min-w-0 flex-col gap-3">
          <p>{examples[rule.id]}</p>
          <p className="text-xs text-muted-foreground">字段：{rule.fields.map(researchLabel).join("、")}；{rule.case_sensitive ? "区分大小写" : "忽略大小写"}</p>
          <code className="block whitespace-pre-wrap break-all rounded-md bg-muted p-3 text-xs">{rule.effective_pattern ?? rule.pattern}</code>
          {rule.requires_context ? <p className="break-all text-xs">还须匹配上下文：<code>{rule.requires_context}</code></p> : null}
          {rule.exclusions.map((exclusion) => <div key={exclusion.id}><p>{exclusion.reason}</p><code className="break-all text-xs">{exclusion.pattern}</code></div>)}
        </div>
      </details>)}</div>
      <p>本轮适配：原规则明确实体瘤或实体瘤边界 → 实体瘤；明确血液肿瘤 → 血液肿瘤；其他结果暂未完成。没有关键词命中不能证明“非肿瘤”。</p>
      <details><summary className="cursor-pointer font-medium">真实命中与漏判例子</summary><ul className="mt-3 flex list-disc flex-col gap-2 pl-5">{data.examples.map((item) => <li key={item.pmid}>PMID {item.pmid}：{item.note}</li>)}</ul></details>
      <details><summary className="cursor-pointer font-medium">匹配与排除的具体执行方式</summary><div className="mt-3"><TextList items={data.engine_notes} /></div></details>
    </CardContent>
  </Card>;
}

function agreement(matched: number, denominator: number) {
  return denominator > 0 ? `${matched}/${denominator}（${(matched / denominator * 100).toFixed(1)}%）` : "未评分";
}

function recall(value: number | null) {
  return value === null ? "未计算" : `${(value * 100).toFixed(1)}%`;
}

function change(value: number | null) {
  return value === null ? "未计算" : `${value > 0 ? "+" : ""}${value.toFixed(1)} 个百分点`;
}

function GroupComparison({ groups }: { groups: NonNullable<ModelComparison["groups"]> }) {
  const entries = Object.entries(groups);
  if (!entries.length) return null;
  return <Card className="min-w-0">
    <CardHeader>
      <CardTitle>原样本与新增样本 · 分开计分</CardTitle>
      <CardDescription>新增组单独报告，避免总体成绩掩盖新样本表现。这是按类别与边界分层扩展的开发集，仍不能代表全库随机样本或生产验收结果。</CardDescription>
    </CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-4">
      <Table aria-label="原样本与新增样本分类成绩">
        <TableHeader><TableRow>
          <TableHead>样本组</TableHead><TableHead>模型</TableHead><TableHead>与参考一致</TableHead><TableHead>三类召回均值</TableHead>
          <TableHead>实体瘤</TableHead><TableHead>血液肿瘤</TableHead><TableHead>非肿瘤</TableHead><TableHead>肿瘤误筛</TableHead><TableHead>失败 / 未完成</TableHead>
        </TableRow></TableHeader>
        <TableBody>{entries.flatMap(([id, group]) => group.methods.map((method) => <TableRow key={`${id}-${method.id}`}>
          <TableCell>{group.label}</TableCell><TableCell>{method.label}</TableCell>
          <TableCell>{agreement(method.matched, method.denominator)}</TableCell><TableCell>{recall(method.macro_recall)}</TableCell>
          {["solid", "hematologic", "non_neoplastic"].map((category) => <TableCell key={category}>{method.by_class[category].matched}/{method.by_class[category].denominator}</TableCell>)}
          <TableCell>{method.tumor_false_exclusions}</TableCell><TableCell>{method.failed} / {method.abstained}</TableCell>
        </TableRow>))}</TableBody>
      </Table>
      <p className="text-xs text-muted-foreground">{entries.map(([, group]) => `${group.label}：${group.denominator} 篇确定参考，${group.unscored} 篇未定`).join("；")}。失败和未完成保留在各组确定参考分母；肿瘤误筛指实体瘤或血液肿瘤被判成非肿瘤。缺少任一类别参考时不计算三类召回均值。显存与耗时仍在总体表展示。</p>
    </CardContent>
  </Card>;
}

function ComparisonTable({ data }: { data: ModelComparison }) {
  return <Card className="min-w-0">
    <CardHeader><CardTitle>同一批文献 · 模型比较</CardTitle><CardDescription>分数与资源占用来自本机实测；三类召回均值对类别等权计算。</CardDescription></CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-4">
      <Table aria-label="模型准确率速度显存比较">
        <TableHeader><TableRow>
          <TableHead>模型 / 参数</TableHead><TableHead>与参考一致</TableHead><TableHead>三类召回均值</TableHead>
          <TableHead>实体瘤</TableHead><TableHead>血液肿瘤</TableHead><TableHead>非肿瘤</TableHead>
          <TableHead>平均秒/篇</TableHead><TableHead>P95 秒/篇</TableHead><TableHead>进程显存峰值</TableHead>
        </TableRow></TableHeader>
        <TableBody>{data.methods.map((method) => <TableRow key={method.id}>
          <TableCell><strong>{method.label}</strong><p className="text-xs text-muted-foreground">{(method.parameters / 1e9).toFixed(2)}B · {method.precision}</p></TableCell>
          <TableCell>{agreement(method.matched, method.denominator)}</TableCell>
          <TableCell>{recall(method.macro_recall)}</TableCell>
          {["solid", "hematologic", "non_neoplastic"].map((category) => <TableCell key={category}>{method.by_class[category].matched}/{method.by_class[category].denominator}</TableCell>)}
          <TableCell>{method.mean_seconds.toFixed(3)}<p className="text-xs text-muted-foreground">中位数 {method.median_seconds.toFixed(3)}</p></TableCell><TableCell>{method.p95_seconds.toFixed(3)}</TableCell>
          <TableCell>{(method.nvml_process_peak_mib / 1024).toFixed(2)} GiB</TableCell>
        </TableRow>)}</TableBody>
      </Table>
      <p className="font-medium">模型选择与规模变化带来了多少收益？</p>
      <Table aria-label="模型提升与资源代价"><TableHeader><TableRow>
        <TableHead>比较</TableHead><TableHead>参数倍数</TableHead><TableHead>准确率变化</TableHead><TableHead>三类召回变化</TableHead>
        <TableHead>逐篇变化</TableHead><TableHead>耗时倍数</TableHead><TableHead>显存倍数</TableHead>
      </TableRow></TableHeader><TableBody>{data.pairwise.map((pair) => <TableRow key={`${pair.before}-${pair.after}`}>
        <TableCell><p>{pair.before} → {pair.after}</p><p className="text-xs text-muted-foreground">{pair.question}</p></TableCell>
        <TableCell>{pair.parameter_ratio.toFixed(2)}×</TableCell>
        <TableCell>{change(pair.accuracy_delta_pp)}</TableCell>
        <TableCell>{change(pair.macro_recall_delta_pp)}</TableCell>
        <TableCell>改对 {pair.improved} 篇 / 改错 {pair.regressed} 篇</TableCell>
        <TableCell>{pair.time_ratio.toFixed(2)}×</TableCell><TableCell>{pair.memory_ratio.toFixed(2)}×</TableCell>
      </TableRow>)}</TableBody></Table>
      <p className="text-xs text-muted-foreground">倍数均为后者 ÷ 前者；耗时或显存小于 1 表示更省。此处是开发集观察结果，不是全库提升承诺。</p>
      <TextList items={data.notes} />
    </CardContent>
  </Card>;
}

function ComparisonCases({ data }: { data: ModelComparison }) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [group, setGroup] = useState("all");
  const [selected, setSelected] = useState("");
  const query = search.trim().toLowerCase();
  const rows = data.cases.filter((row) => (!query || `${row.pmid} ${row.title}`.toLowerCase().includes(query))
    && (group === "all" || row.group === group)
    && (filter === "all" || row.predictions.some((p) => p.outcome !== "matched" && p.outcome !== "unscored")));
  const active = rows.find((row) => row.pmid === selected) ?? rows[0];
  return <div className="flex min-w-0 flex-col gap-4">
    <FieldGroup className="md:flex-row">
      <Field><FieldLabel htmlFor="comparison-search">查找比较文献</FieldLabel><Input id="comparison-search" value={search} placeholder="标题或 PMID" onChange={(event) => setSearch(event.target.value)} /></Field>
      {data.groups && Object.keys(data.groups).length ? <Field><FieldLabel htmlFor="comparison-group">样本组</FieldLabel>
        <Select value={group} onValueChange={setGroup}><SelectTrigger id="comparison-group"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
          <SelectItem value="all">全部样本</SelectItem>
          {Object.entries(data.groups).map(([id, item]) => <SelectItem key={id} value={id}>{item.label}</SelectItem>)}
        </SelectGroup></SelectContent></Select>
      </Field> : null}
      <Field><FieldLabel htmlFor="comparison-filter">显示范围</FieldLabel>
        <Select value={filter} onValueChange={setFilter}><SelectTrigger id="comparison-filter"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>
          <SelectItem value="all">全部文献</SelectItem><SelectItem value="disagreements">至少一个模型错误或未完成</SelectItem>
        </SelectGroup></SelectContent></Select>
      </Field>
    </FieldGroup>
    <p className="text-xs text-muted-foreground">显示 {rows.length} / {data.cases.length} 篇；参考未定的文献不计入准确率。</p>
    <Table aria-label="模型逐篇分类对照"><TableHeader><TableRow><TableHead>文献</TableHead><TableHead>Astra 参考</TableHead>{data.methods.map((method) => <TableHead key={method.id}>{method.label}</TableHead>)}</TableRow></TableHeader>
      <TableBody>{rows.map((row) => <TableRow key={row.pmid} data-state={active?.pmid === row.pmid ? "selected" : undefined}>
        <TableCell className="max-w-sm whitespace-normal"><button type="button" className="text-left hover:underline" onClick={() => setSelected(row.pmid)} aria-pressed={active?.pmid === row.pmid}>{row.title}<span className="block text-xs text-muted-foreground">PMID {row.pmid}{row.group && data.groups?.[row.group] ? ` · ${data.groups[row.group].label}` : ""}</span></button></TableCell>
        <TableCell>{row.reference ? researchLabel(row.reference) : "参考未定"}</TableCell>
        {row.predictions.map((prediction) => <TableCell key={prediction.method_id}>
          <p>{prediction.category ? researchLabel(prediction.category) : "未完成"}</p>
          <Badge variant={prediction.outcome === "disagreed" || prediction.outcome === "failed" ? "destructive" : "secondary"}>{researchLabel(prediction.outcome)}</Badge>
        </TableCell>)}
      </TableRow>)}</TableBody>
    </Table>
    {active ? <Card>
      <CardHeader><CardTitle>PMID {active.pmid} · 参考依据与原始输出</CardTitle><CardDescription>{active.title}</CardDescription></CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-4">
        <p>{active.reference_notes}</p><Quotes quotes={active.reference_quotes} />
        {active.predictions.map((prediction) => <details key={prediction.method_id}><summary className="cursor-pointer">{prediction.label} · {researchLabel(prediction.status)} · {prediction.seconds.toFixed(3)} 秒</summary><pre className="mt-2 whitespace-pre-wrap break-all text-xs">{prediction.raw_output}</pre></details>)}
      </CardContent>
    </Card> : <NoData title="没有匹配的文献" description="请调整查找或筛选条件。" />}
  </div>;
}

function Experiment({ id }: { id: string }) {
  const experiment = useCuration<ResearchExperiment>(`research/${encodeURIComponent(id)}/`);
  const data = experiment.data;
  if (experiment.error) return <ErrorMessage error={experiment.error} />;
  if (!data) return <div className="flex flex-col gap-3" role="status" aria-label="正在读取研究记录"><Skeleton className="h-24 w-full" /><Skeleton className="h-64 w-full" /></div>;
  return <article className="flex min-w-0 flex-col gap-5" aria-label="实验记录">
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2"><Badge variant="secondary">{researchLabel(data.status)}</Badge><span className="text-xs text-muted-foreground">{data.date} · {data.dataset.document_count ? `${data.dataset.document_count} 篇文献 · ` : ""}{data.sample_count} {data.sample_unit ?? "篇"}</span></div>
      <h2 className="text-xl font-semibold">{data.title}</h2>
      <p className="text-sm/relaxed text-muted-foreground">{data.summary}</p>
    </div>
    {id === "entity-resolution-20261101" ? <Alert><Info /><AlertTitle>历史混合字段指标</AlertTitle><AlertDescription>本记录保留原严格口径。12/19、11/19混合了疾病与药物，并检查多项字段，不能当作癌种主类准确率。<a className="underline underline-offset-4" href="/management/research/?experiment=entity-resolution-business-20261101-v2">查看用户确认的主类业务口径</a>。</AlertDescription></Alert> : null}
    {data.entity_resolution?.scoring === "business-v2" ? <Alert><Info /><AlertTitle>主类正确与字段完整度分开</AlertTitle><AlertDescription>癌种主类正确即可通过核心判断；亚型、分期、限定和原文细节另记完整度。14个疾病、5个药物、1个DMG挑战分别计分，未重新调用模型。<a className="underline underline-offset-4" href={`/management/research/?experiment=${data.entity_resolution.previous_record}`}>查看原严格口径历史记录</a>。</AlertDescription></Alert> : null}
    <Card>
      <CardHeader>
        <CardTitle>本次到底在判断什么？</CardTitle>
        <CardDescription>{data.task.question}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p><strong>输入：</strong>{data.task.input}</p>
        <div><p className="mb-2 font-medium">要求输出</p><TextList items={data.task.outputs} /></div>
        {data.classification_definition ? <details className="rounded-lg border p-3">
          <summary className="cursor-pointer font-medium">本轮三分类的边界定义</summary>
          <div className="mt-3"><TextList items={data.classification_definition.boundary_notes} /></div>
        </details> : null}
        <div><p className="mb-2 font-medium">本次尚未评估</p><TextList items={data.task.not_measured} /></div>
      </CardContent>
      <CardFooter><p className="text-muted-foreground">{data.dataset.label} · {researchLabel(data.dataset.role)}</p></CardFooter>
    </Card>
    {id === "scope-pilot-20261022" ? <Alert>
      <Info />
      <AlertTitle>0 分不等于每个局部判断都错</AlertTitle>
      <AlertDescription>
        本轮同时要求九类相关性、多轴标签和原文引用。主类成绩要求整份输出先通过校验，再与参考一致；
        原始输出中有一篇“非肿瘤”主类判断正确，但另一项标签不合法，整份未通过。多轴标签须单独评估，不能由主类的 0 分推断全部错误。
      </AlertDescription>
    </Alert> : null}
    <Tabs defaultValue={data.task_kind === "target_compatibility" ? "cases" : "methods"}>
      <TabsList className="max-w-full">
        <TabsTrigger value="methods">方法与结论</TabsTrigger>
        <TabsTrigger value="cases">{data.task_kind === "entity_resolution" ? `逐个原词（${data.entity_resolution?.cases.length ?? 0}）` : data.task_kind === "target_compatibility" ? `逐条兼容核对（${data.target_compatibility?.records.length ?? 0}）` : data.task_kind === "knowledge_candidates" ? `逐条知识（${data.knowledge_candidates?.length ?? 0}）` : `逐篇对照${data.comparison ? `（${data.comparison.cases.length}）` : data.cases ? `（${data.cases.length}）` : ""}`}</TabsTrigger>
        <TabsTrigger value="provenance">版本与来源</TabsTrigger>
      </TabsList>
      <TabsContent value="methods" className="flex flex-col gap-5 pt-3">
        <Alert>
          <FlaskConical />
          <AlertTitle>{data.task_kind === "target_compatibility" ? "核对范围" : "比较范围"}</AlertTitle>
          <AlertDescription>{data.reference.label}：{data.reference.note}{data.task_kind !== "target_compatibility" ? " 不同实验的样本、任务或参考版本不同时，不能直接按百分比排名。" : ""}</AlertDescription>
        </Alert>
        {data.comparison ? <ComparisonTable data={data.comparison} /> : null}
        {data.comparison?.groups ? <GroupComparison groups={data.comparison.groups} /> : null}
        <details open={!data.comparison} className="min-w-0 rounded-lg border p-4">
          <summary className="cursor-pointer font-medium">{data.task_kind === "target_compatibility" ? "本轮适配统计与判定口径" : "各方法的详细指标与计分口径"}</summary>
          <div className="mt-4 grid min-w-0 gap-4 lg:grid-cols-2">
            {data.methods.map((method) => <Card key={method.id} className="min-w-0">
              <CardHeader><CardTitle>{method.label}</CardTitle><CardDescription className="break-all">{method.version}</CardDescription></CardHeader>
              <CardContent className="flex flex-col divide-y">{method.metrics.map((metric, index) => <Metric metric={metric} key={`${metric.label}-${index}`} />)}</CardContent>
              <CardFooter><p>{method.conclusion}</p></CardFooter>
            </Card>)}
          </div>
        </details>
        {data.rules_explanation ? <RuleExplanation data={data.rules_explanation} /> : null}
        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader><CardTitle>留下的研究经验</CardTitle><CardDescription>保存失败原因与适用条件，后续先查记录再决定重跑。</CardDescription></CardHeader>
            <CardContent><TextList items={data.findings} /></CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>下一步安排</CardTitle><CardDescription>以下为后续计划，未列入本次实测成绩。</CardDescription></CardHeader>
            <CardContent><TextList items={data.next_steps} /></CardContent>
          </Card>
        </div>
      </TabsContent>
      <TabsContent value="cases" className="pt-3">
        {data.task_kind === "entity_resolution" ? data.entity_resolution ? <EntityResolutionCases cases={data.entity_resolution.cases} business={data.entity_resolution.scoring === "business-v2"} /> : <NoData title="实体对齐实验尚无逐项结果" description="仅展示实际完成的实验产物。" /> : data.task_kind === "target_compatibility" ? data.target_compatibility ? <TargetCompatibilityPanel data={data.target_compatibility} /> : <NoData title="兼容核对记录尚未提供" description="只展示实际生成的适配结果。" /> : data.task_kind === "knowledge_candidates" ? data.knowledge_candidates?.length ? <KnowledgeCandidates records={data.knowledge_candidates} /> : <NoData title="该记录没有公开的知识候选" description="现有汇总指标仍保留；未补造实体、原文引用或映射结果。" /> : data.comparison ? <ComparisonCases data={data.comparison} /> : data.cases?.length ? <Cases key={data.id} records={data.cases} threeClass={data.task_kind === "three_class"} /> : <NoData title="该记录没有公开的逐篇对照" description="现有历史记录与汇总指标仍保留；未补造个案或重新运行实验。" />}
      </TabsContent>
      <TabsContent value="provenance" className="flex flex-col gap-5 pt-3">
        <Card>
          <CardHeader><CardTitle>样本与参考边界</CardTitle><CardDescription>{data.dataset.label}</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-4"><TextList items={data.dataset.notes} /><p>{data.reference.label} · {researchLabel(data.reference.status)}</p><p>{data.reference.note}</p></CardContent>
        </Card>
        <details className="rounded-lg border p-4">
          <summary className="cursor-pointer font-medium">展开任务、样本、方法与参考版本 · {researchLabel(data.identity.completeness)}</summary>
          <dl className="mt-4 grid min-w-0 gap-4 sm:grid-cols-2">
            {[
              ["任务版本", [data.identity.task_version]],
              ["样本版本", data.identity.sample_versions],
              ["方法版本", data.identity.method_versions],
              ["参考版本", data.identity.reference_versions],
              ["实验指纹", [data.fingerprint]],
            ].map(([label, values]) => <div key={label as string} className="min-w-0">
              <dt className="mb-1 text-muted-foreground">{label}</dt>
              <dd className="break-all font-mono text-xs/relaxed">{(values as string[]).join("\n") || "未记录"}</dd>
            </div>)}
          </dl>
        </details>
        <details className="rounded-lg border p-4">
          <summary className="cursor-pointer font-medium">展开来源记录（{data.provenance.length}）</summary>
          <div className="mt-4 flex min-w-0 flex-col gap-4">{data.provenance.map((source, index) => <div key={index} className="min-w-0">
            <p className="font-medium">{source.label}</p><p className="break-all text-xs/relaxed">{source.artifact}</p>
            <p className="break-all font-mono text-xs/relaxed text-muted-foreground">SHA-256：{source.sha256 || "历史记录未提供"}</p>
          </div>)}</div>
        </details>
      </TabsContent>
    </Tabs>
  </article>;
}

export default function ResearchHistoryApp() {
  const index = useCuration<ResearchIndex>("research/");
  const [stage, setStage] = useState("all");
  const [selected, setSelected] = useState(() => new URLSearchParams(location.search).get("experiment") ?? "");
  const data = index.data;
  const experiments = data?.experiments.filter((item) => stage === "all" || item.stage === stage) ?? [];
  const selectedId = experiments.some((item) => item.id === selected) ? selected
    : experiments.find((item) => item.id === data?.default_experiment)?.id ?? experiments[0]?.id;
  const currentStage = data?.stages.find((item) => item.id === stage);
  function choose(id: string) {
    setSelected(id);
    const url = new URL(location.href);
    url.searchParams.set("experiment", id);
    history.replaceState(null, "", url);
  }
  return <AppShell title="研究历程" description="记录每一步试过的方法、样本与参考版本、实测结果和下一步，避免重复试验。" crumbs={[{ label: "管理" }, { label: "研究历程" }]}>
    <ErrorMessage error={index.error} />
    {index.loading ? <Skeleton className="h-36 w-full" role="status" aria-label="正在读取研究历程" /> : null}
    {data ? <>
      <Card>
        <CardHeader>
          <CardTitle>{data.current_task.title}</CardTitle>
          <CardDescription>{researchLabel(data.current_task.status)}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap gap-2">{data.current_task.categories.map((category) => <Badge key={category}>{category}</Badge>)}</div>
          <p>{data.current_task.note}</p>
        </CardContent>
      </Card>
      <FieldGroup className="md:flex-row">
        <Field className="min-w-0 md:max-w-xs">
          <FieldLabel htmlFor="research-stage">研究环节</FieldLabel>
          <Select value={stage} onValueChange={setStage}>
            <SelectTrigger id="research-stage"><SelectValue /></SelectTrigger>
            <SelectContent><SelectGroup>
              <SelectItem value="all">全部环节</SelectItem>
              {data.stages.map((item) => <SelectItem value={item.id} key={item.id}>{item.label}</SelectItem>)}
            </SelectGroup></SelectContent>
          </Select>
        </Field>
        {selectedId ? <Field className="min-w-0">
          <FieldLabel htmlFor="research-experiment">查看实验记录</FieldLabel>
          <Select value={selectedId} onValueChange={choose}>
            <SelectTrigger id="research-experiment" className="w-full min-w-0 max-w-full"><SelectValue className="min-w-0 truncate" /></SelectTrigger>
            <SelectContent><SelectGroup>{experiments.map((item) => <SelectItem value={item.id} key={item.id}>{item.date} · {item.title}</SelectItem>)}</SelectGroup></SelectContent>
          </Select>
        </Field> : null}
      </FieldGroup>
      {currentStage ? <Alert><Info /><AlertTitle>{currentStage.label} · {researchLabel(currentStage.status)}</AlertTitle><AlertDescription>{currentStage.note}</AlertDescription></Alert> : null}
      <details className="rounded-lg border p-4">
        <summary className="cursor-pointer text-sm font-medium">研究记录时间线（当前环节 {experiments.length} 项 / 全部 {data.experiments.length} 项）</summary>
        <ol className="mt-4 flex flex-col gap-4">
          {experiments.map((item) => <li key={item.id} className="flex flex-wrap items-start gap-3 border-l-2 pl-4">
            <div className="min-w-0 flex-1"><p className="text-xs text-muted-foreground">{item.date} · {item.sample_count} {item.sample_unit ?? "篇"} · {researchLabel(item.status)}</p><h3 className="mt-1 text-sm font-medium">{item.title}</h3><p className="mt-1 text-xs/relaxed text-muted-foreground">{item.summary}</p></div>
            <Button variant="outline" size="sm" onClick={() => choose(item.id)} disabled={selectedId === item.id}>{selectedId === item.id ? "当前记录" : "查看记录"}</Button>
          </li>)}
        </ol>
      </details>
      {selectedId ? <Experiment id={selectedId} key={selectedId} /> : <NoData title="这个环节还没有实测记录" description="环节计划已保留。后续实验完成并登记后，才会显示方法、指标和结论。" />}
    </> : null}
  </AppShell>;
}
