import { useState } from "react";
import { ArrowUpRight, Info } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { NoData } from "@/components/curation/Common";
import { researchLabel, type TargetCompatibilityPreview, type TargetCompatibilityRecord } from "@/lib/research";

const STATUSES: Record<string, string> = {
  offline_validated: "离线结构检查通过", pending_mapping: "映射尚未完成",
  not_representable: "当前目标结构不能表达", invalid_payload: "目标字段待修正", invalid_source: "来源待修正",
};
const MAPPING: Record<string, string> = {
  exact_target_name: "目标同名唯一匹配", explicit_crosswalk: "本次提议的癌种对应",
  ambiguous: "存在多个目标 ID", not_found: "当前规则未匹配", unmapped: "当前规则未匹配", not_identity: "未确定具体身份",
  confirmed_local_target_id: "复用已确认的本地对应", confirmed_local_unique_target_name: "本地标准名唯一对应",
  unique_snapshot_name: "目标名称唯一对应", local_rule_unresolved: "本地对应规则待确定",
  source_explicit_qualified_target_name: "保留限定后的完整癌种对应",
  source_defined_qualified_abbreviation: "复用本篇明示的癌种缩写",
  local_identity_target_missing: "本地身份已确定，缺目标对应",
  target_class_identity_missing: "本地类别已确定，缺目标类别对应",
  target_identity_missing: "快照未覆盖唯一身份", target_addition_request: "需要新增目标实体",
  target_name_ambiguous: "目标名称存在歧义", target_uuid_not_in_snapshot: "目标 ID 未在快照中",
  bilingual_reference: "已有双语参考对应主癌种",
};
const QUALITY: Record<string, string> = { valid: "有效", invalid: "无效", unreviewed: "未核验", undetermined: "未确定" };
const SCOPE_KINDS: Record<string, string> = {
  direct: "明确的分子条件",
  none: "没有此类条件", unresolved: "尚未确定", unsupported_marker: "当前目标不支持此类标志物条件",
  alternatives: "备选方案", combination: "同时联合", sequential: "序贯治疗", single: "单一治疗", unspecified: "逻辑尚未明确",
};

function JsonDetail({ title, value }: { title: string; value: unknown }) {
  return <details className="min-w-0 rounded-lg border p-3">
    <summary className="cursor-pointer text-sm">{title}</summary>
    <pre className="mt-3 whitespace-pre-wrap break-all text-xs/relaxed">{JSON.stringify(value, null, 2)}</pre>
  </details>;
}

function Scope({ title, scope }: { title: string; scope: Record<string, unknown> }) {
  return <section className="flex min-w-0 flex-col gap-2">
    <h4 className="font-medium">{title}</h4>
    <p className="text-sm/relaxed">{typeof scope.reason === "string" ? scope.reason : "未提供明确的适配依据。"}</p>
    <p className="text-xs text-muted-foreground">类型：{SCOPE_KINDS[String(scope.kind)] ?? String(scope.kind ?? "未确定")}{scope.logic ? ` · 逻辑：${String(scope.logic)}` : ""}</p>
    <JsonDetail title={`展开${title}与原文依据`} value={scope} />
  </section>;
}

function CompatibilityRecord({ row }: { row: TargetCompatibilityRecord }) {
  const [expanded, setExpanded] = useState(false);
  const dimensions = row.quality.quality_dimensions;
  return <Card className="min-w-0" aria-label={`${row.group_id} 目标兼容预览`}>
    <CardHeader>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <CardTitle>{row.group_id}</CardTitle>
        <div className="flex flex-wrap gap-2">
          <Badge variant="secondary">{STATUSES[row.compatibility_status] ?? row.compatibility_status}</Badge>
          <Badge variant="outline">独立质量：{QUALITY[row.quality.status] ?? row.quality.status}</Badge>
          <Badge variant="outline">真实报告匹配：{row.live_sample_matching === "unverified" ? "未验证" : row.live_sample_matching}</Badge>
          <Badge variant="outline">未导入</Badge>
        </div>
      </div>
      <CardDescription className="break-words">{row.article.title} · PMID {row.article.pmid}</CardDescription>
    </CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-4">
      <p className="text-sm/relaxed">{String(row.fact.conclusion || row.fact.treatment || row.fact.population || "原知识未提供简短结论，请展开查看原始字段。")}</p>
      <p className="text-xs text-muted-foreground">预览采用目标 ID {row.entity_mappings.filter((item) => item.accepted_by_exporter).length} / {row.entity_mappings.length} 项 · 适配问题 {row.mapping_issues.length} 项</p>
      {expanded ? <div id={`target-detail-${row.id}`} className="flex min-w-0 flex-col gap-5">
        <section className="flex flex-col gap-2" aria-label={`${row.group_id} 独立质量`}>
          <h4 className="font-medium">独立语义质量</h4>
          <p className="text-sm/relaxed">{row.quality.reason || "本轮仅核对目标 ID 与结构，未重新评判知识是否正确。"}</p>
          {dimensions ? <p className="text-xs/relaxed">核心有效：{dimensions.core_valid ? "是" : "否"} · 参与实体可用：{dimensions.participants_usable ? "是" : "否"} · 原定目标覆盖：{dimensions.target_covered ? "是" : "否"}</p> : null}
        </section>
        <section className="flex min-w-0 flex-col gap-3" aria-label={`${row.group_id} 目标实体映射`}>
          <h4 className="font-medium">目标药物与癌种 ID</h4>
          <Table>
            <TableHeader><TableRow><TableHead>原知识实体／角色</TableHead><TableHead>目标名称与真实 UUID</TableHead><TableHead>映射依据与状态</TableHead></TableRow></TableHeader>
            <TableBody>{row.entity_mappings.map((item) => <TableRow key={item.entity_ref}>
              <TableCell className="max-w-64 whitespace-normal break-words">{item.label}<span className="block text-xs text-muted-foreground">{researchLabel(item.kind)} · {item.roles.join("、")}</span></TableCell>
              <TableCell className="max-w-80 whitespace-normal break-words">
                {item.target_id ? <>{item.target_name_cn || item.target_name}<span className="block break-all font-mono text-xs">{item.target_id}</span></> : "未确定唯一目标 ID"}
                {!item.target_id && item.candidates?.length ? <details className="mt-2"><summary className="cursor-pointer text-xs">查看未采用的候选（{item.candidates.length}）</summary>{item.candidates.map((candidate, index) => <p key={index} className="mt-2 break-all text-xs">{candidate.name_cn || candidate.name || candidate.name_en}<br />{candidate.id || candidate.target_id}</p>)}</details> : null}
              </TableCell>
              <TableCell className="max-w-80 whitespace-normal break-words"><Badge variant="outline">{MAPPING[item.status] ?? item.status}</Badge><p className="mt-1 text-xs/relaxed">{item.basis}</p><p className="mt-1 text-xs text-muted-foreground">导出器：{item.accepted_by_exporter ? item.status === "explicit_crosswalk" ? "预览采用此 ID，待用户核对" : "预览采用此 ID" : "未采用为确定 ID"}</p></TableCell>
            </TableRow>)}</TableBody>
          </Table>
          {!row.entity_mappings.length ? <p className="text-sm text-muted-foreground">本条没有可映射的明确药物或癌种参与者。</p> : null}
        </section>
        <div className="grid min-w-0 gap-5 lg:grid-cols-2"><Scope title="分子触发条件" scope={row.trigger_scope} /><Scope title="多药与治疗逻辑" scope={row.treatment_scope} /></div>
        <section className="flex min-w-0 flex-col gap-2" aria-label={`${row.group_id} 适配缺口`}>
          <h4 className="font-medium">具体适配缺口</h4>
          {row.review_points.map((note, index) => <p key={index} className="text-sm/relaxed">{note}</p>)}
          {row.mapping_issues.map((issue, index) => <div key={index} className="text-xs/relaxed"><p className="break-all font-mono">{issue.code} · {issue.path}</p><p>{issue.detail}</p></div>)}
          {!row.mapping_issues.length ? <p>离线适配未发现缺口；仍未执行目标导入。</p> : null}
        </section>
        <section className="flex min-w-0 flex-col gap-3" aria-label={`${row.group_id} 文章与原文`}>
          <h4 className="font-medium">文章与原文出处</h4>
          <a className="break-words text-sm hover:underline" href={`https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(row.article.pmid)}/`} target="_blank" rel="noreferrer">{row.article.title} · PMID {row.article.pmid} ↗</a>
          {row.quotes.map((quote, index) => <figure key={index}><blockquote className="break-words border-l-2 pl-3 text-sm/relaxed">{quote.quote}</blockquote><figcaption className="mt-1 break-all text-xs text-muted-foreground">{quote.locator} · 字符 [{quote.start}, {quote.end}) · 段落 {quote.passage_id}</figcaption></figure>)}
        </section>
        <JsonDetail title="原知识完整字段" value={row.fact} />
        <JsonDetail title="目标兼容包字段（仅预览）" value={row.target_payload} />
        <JsonDetail title="离线结构与匹配检查详情" value={{ contract: row.contract_validation, matching: row.matching_validation, live_sample_matching: row.live_sample_matching }} />
      </div> : null}
    </CardContent>
    <CardFooter className="flex flex-wrap gap-2">
      <Button size="sm" variant="outline" onClick={() => setExpanded(!expanded)} aria-expanded={expanded} aria-controls={expanded ? `target-detail-${row.id}` : undefined}>{expanded ? "收起" : "展开"} {row.group_id} 核对详情</Button>
      {row.candidate_id ? <Button size="sm" variant="outline" asChild><a href={`/knowledge/candidates/?candidate=${encodeURIComponent(row.candidate_id)}`}>查看已有知识 <ArrowUpRight data-icon="inline-end" /></a></Button>
        : <span className="text-xs text-muted-foreground">离线研究记录，尚未写入候选库</span>}
    </CardFooter>
  </Card>;
}

export function TargetCompatibilityPanel({ data }: { data: TargetCompatibilityPreview }) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [batch, setBatch] = useState("all");
  const [page, setPage] = useState(0);
  const search = query.trim().toLowerCase();
  const rows = data.records.filter((row) => (status === "all" || row.compatibility_status === status)
    && (batch === "all" || row.source_run === batch)
    && (!search || `${row.group_id} ${row.article.title} ${row.article.pmid} ${row.entity_mappings.map((item) => `${item.label} ${item.target_name ?? ""} ${item.target_name_cn ?? ""} ${item.target_id ?? ""}`).join(" ")}`.toLowerCase().includes(search)));
  const pageSize = 20;
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const currentPage = Math.min(page, pageCount - 1);
  const visibleRows = rows.slice(currentPage * pageSize, (currentPage + 1) * pageSize);
  return <div className="flex min-w-0 flex-col gap-4">
    <Alert><Info /><AlertTitle>只读目标兼容预览 · 未导入 tumor_api</AlertTitle><AlertDescription>真实 UUID 来自目标库只读快照。ID 映射、结构可表达性和知识是否正确是三件不同的事；缺少触发条件或多药逻辑尚未适配，不等于原知识无效。此页没有导入操作。</AlertDescription></Alert>
    <FieldGroup className="md:flex-row">
      <Field><FieldLabel htmlFor="target-preview-search">查找知识或目标实体</FieldLabel><Input id="target-preview-search" placeholder="组号、PMID、药物、癌种或 UUID" value={query} onChange={(event) => { setQuery(event.target.value); setPage(0); }} /></Field>
      <Field><FieldLabel htmlFor="target-preview-status">结构适配状态</FieldLabel><Select value={status} onValueChange={(value) => { setStatus(value); setPage(0); }}><SelectTrigger id="target-preview-status"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="all">全部状态</SelectItem>{[...new Set(data.records.map((row) => row.compatibility_status))].map((value) => <SelectItem key={value} value={value}>{STATUSES[value] ?? value}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
      <Field><FieldLabel htmlFor="target-preview-batch">来源批次</FieldLabel><Select value={batch} onValueChange={(value) => { setBatch(value); setPage(0); }}><SelectTrigger id="target-preview-batch"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="all">全部批次</SelectItem>{[...new Set(data.records.map((row) => row.source_run))].map((value) => <SelectItem key={value} value={value}>{data.batch_labels?.[value] ?? (value === "minimal-e2e-20261101" ? "新 5 篇 · 未核验" : value === "minimal-knowledge-selected-20261101" ? "原 5 篇 · 已有独立参考" : value)}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
    </FieldGroup>
    <p className="text-xs text-muted-foreground" aria-live="polite">匹配 {rows.length} / {data.records.length} 条{rows.length ? ` · 当前第 ${currentPage * pageSize + 1}–${currentPage * pageSize + visibleRows.length} 条` : ""} · 目标字典快照 {data.dictionary.snapshot_id}</p>
    {rows.length ? visibleRows.map((row) => <CompatibilityRecord key={row.id} row={row} />) : <NoData title="没有匹配的知识" description="调整状态、批次或查找条件。" />}
    {pageCount > 1 ? <nav className="flex flex-wrap items-center gap-3" aria-label="兼容核对分页">
      <Button variant="outline" size="sm" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>上一页</Button>
      <span className="text-sm text-muted-foreground">第 {currentPage + 1} / {pageCount} 页</span>
      <Button variant="outline" size="sm" disabled={currentPage + 1 === pageCount} onClick={() => setPage(currentPage + 1)}>下一页</Button>
    </nav> : null}
  </div>;
}
