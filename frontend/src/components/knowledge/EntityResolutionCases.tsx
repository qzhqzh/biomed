import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { EntityResolutionCase } from "@/lib/research";

const DECISIONS: Record<string, string> = { MATCH: "对应已有实体", NEW: "新增实体", UNRESOLVED: "保留待定", IGNORE: "不归入当前实体范围", NOT_ENTITY: "不归入当前实体范围", AMBIGUOUS: "歧义待定" };

export function EntityResolutionCases({ cases, business = false }: { cases: EntityResolutionCase[]; business?: boolean }) {
  const [errorsOnly, setErrorsOnly] = useState(false);
  const rows = errorsOnly ? cases.filter((row) => row.methods.some((method) => !method.correct)) : cases;
  return <div className="flex min-w-0 flex-col gap-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><p className="text-sm text-muted-foreground">{business ? "癌种按主类评价，药物身份单独评价；亚型、限定和出处完整度另列，不否决主类正确。" : "历史严格口径：同时检查实体身份、层级和必要限定。"}</p><Button size="sm" variant="outline" aria-pressed={errorsOnly} onClick={() => setErrorsOnly(!errorsOnly)}>{errorsOnly ? "显示全部原词" : business ? "只看核心未对齐" : "只看有分歧的原词"}</Button></div>
    <p className="text-sm text-muted-foreground">当前显示 {rows.length}／{cases.length} 项</p>
    {rows.map((row) => <Card key={row.id} className="min-w-0"><CardHeader><div className="flex flex-wrap items-center gap-2"><Badge variant="outline">{row.kind === "drug" ? "药物" : "癌种／疾病"}</Badge><Badge variant="secondary">{row.cohort === "known_risky_synonym_challenge" ? "独立DMG挑战" : "真实待解原词"}</Badge><span className="text-xs text-muted-foreground">{row.id}</span></div><CardTitle className="break-words">{row.label}</CardTitle><CardDescription>{row.reference_note}</CardDescription></CardHeader><CardContent className="flex min-w-0 flex-col gap-3">
      {business && row.kind === "disease" ? <p className="text-sm">主癌种参考：{row.reference_main || "未确定"}</p> : <p className="text-sm">参考：{DECISIONS[row.reference_decision] || row.reference_decision}{row.reference_entity ? ` · ${row.reference_entity}` : ""}</p>}
      {row.required_qualifiers.length ? <p className="break-words text-sm">{business ? "完整度检查的限定（不否决主类正确）" : "必须保留的限定"}：{row.required_qualifiers.join("；")}</p> : null}
      <Table className={business ? "min-w-[780px]" : "min-w-[620px]"}><TableHeader><TableRow><TableHead>方法</TableHead><TableHead>判断与实体</TableHead><TableHead>{business ? "核心判断" : "参考核对"}</TableHead>{business ? <TableHead>字段完整度</TableHead> : null}<TableHead>耗时</TableHead></TableRow></TableHeader><TableBody>{row.methods.map((method) => <TableRow key={method.id}><TableCell>{method.label}</TableCell><TableCell className="max-w-lg whitespace-normal break-words"><p>{DECISIONS[method.decision] || method.decision} · {method.entity || "无确定实体"}</p><p className="mt-1 text-xs/relaxed text-muted-foreground">{method.reason}</p></TableCell><TableCell className="whitespace-normal"><Badge variant={method.correct ? "secondary" : "outline"}>{business ? method.core_result?.explanation_only ? "解释已识别，字段待组装" : method.core_result?.recovered ? "字典回填后主类正确" : method.correct ? row.kind === "disease" ? "主类正确" : method.decision === "UNRESOLVED" ? "正确保留待定" : "身份判定正确" : "未对齐" : method.correct ? "通过" : "未通过"}</Badge>{business && method.core_result?.explanation_only ? <p className="mt-1 text-xs text-muted-foreground">未计入直接可用主类；不表示医学概念认错。</p> : null}{business && method.core_result?.recovered ? <p className="mt-1 text-xs text-muted-foreground">模型未直接给对主类；用其实体候选回查本地层级。</p> : null}</TableCell>{business ? <TableCell className="min-w-40 whitespace-normal"><div className="flex flex-col gap-1 text-xs">{method.completeness?.map((field) => <p key={field.label}>{field.label}：{field.correct ? "完整" : "待补／待核对"}</p>)}</div></TableCell> : null}<TableCell>{method.seconds === null ? "未测量" : `${method.seconds.toFixed(2)} 秒`}</TableCell></TableRow>)}</TableBody></Table>
      <details className="min-w-0"><summary className="cursor-pointer text-sm">原文依据（{row.quotes.length}）</summary><div className="mt-3 flex flex-col gap-3">{row.quotes.map((quote, index) => <blockquote className="whitespace-pre-wrap break-words rounded-md border p-3 text-sm/relaxed" key={index}>{quote}</blockquote>)}</div></details>
    </CardContent></Card>)}
    <p className="text-xs text-muted-foreground">这里保存冻结的实验结果。后续实体和别名审核请使用<a className="underline underline-offset-4" href="/knowledge/entities/?view=review">本地实体库</a>；不覆盖本次研究记录。</p>
  </div>;
}
