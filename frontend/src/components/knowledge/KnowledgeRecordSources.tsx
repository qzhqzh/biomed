import { useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { STATES } from "@/lib/curation";
import { knowledgeLabel, type KnowledgeAnchor, type KnowledgeArticle, type KnowledgeCandidate } from "@/lib/knowledge-candidates";

const PUBLICATION_LABELS: Record<string, string> = {
  population: "人群／疾病",
  drug_names: "药物／治疗名称",
  conditions: "条件",
  comparator: "比较对象",
  results: "结局与数值",
  scope: "研究范围",
  source_label: "原文名称",
  source_definition: "同篇原文定义",
  project_translation: "项目中文译名",
};

export function knowledgeDisplayLabel(value: string) {
  return PUBLICATION_LABELS[value] ?? knowledgeLabel(value);
}

export function JsonDetails({ title, value }: { title: string; value: unknown }) {
  const [open, setOpen] = useState(false);
  return <details className="min-w-0" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer text-sm">{title}</summary>
    {open ? <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all text-xs/relaxed">{JSON.stringify(value, null, 2)}</pre> : null}
  </details>;
}


export function ArticleSource({ article }: { article: KnowledgeArticle }) {
  return <section aria-label="文章出处" className="flex min-w-0 flex-col gap-2">
    <h3 className="font-medium">文章出处</h3>
    <p className="break-words text-sm/relaxed">{article.title}</p>
    <div className="flex flex-wrap gap-3 text-xs">
      {article.pmid ? <a className="underline" href={`https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(article.pmid)}/`} target="_blank" rel="noreferrer">PMID {article.pmid} ↗</a> : null}
      {article.pmcid ? <a className="underline" href={`https://pmc.ncbi.nlm.nih.gov/articles/${encodeURIComponent(article.pmcid)}/`} target="_blank" rel="noreferrer">{article.pmcid} ↗</a> : null}
      {article.doi ? <a className="break-all underline" href={`https://doi.org/${encodeURIComponent(article.doi)}`} target="_blank" rel="noreferrer">DOI {article.doi} ↗</a> : null}
    </div>
    {article.integrity_status !== "valid" ? <Alert variant="destructive"><AlertTitle>原文版本已隔离</AlertTitle><AlertDescription>以下为保留的历史候选，不代表当前可用证据。</AlertDescription></Alert> : null}
    <details className="min-w-0">
      <summary className="cursor-pointer text-xs text-muted-foreground">文章版本与使用权利</summary>
      <dl className="mt-2 flex flex-col gap-1 break-all text-xs/relaxed">
        <div><dt className="inline font-medium">文章 ID：</dt><dd className="inline">{article.material_id}</dd></div>
        <div><dt className="inline font-medium">版本 ID：</dt><dd className="inline">{article.version_id}</dd></div>
        <div><dt className="inline font-medium">原文件 SHA-256：</dt><dd className="inline">{article.sha256}</dd></div>
        <div><dt className="inline font-medium">权利声明：</dt><dd className="inline whitespace-pre-wrap">{article.rights || "未提供"}</dd></div>
      </dl>
    </details>
  </section>;
}


export function AnchorSource({ anchor }: { anchor: KnowledgeAnchor }) {
  const [fullText, setFullText] = useState(false);
  return <figure className="flex min-w-0 flex-col gap-2">
    <figcaption className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <Badge variant="outline">{knowledgeLabel(anchor.purpose)}</Badge>
      {anchor.entity_ref ? <span>{knowledgeLabel(anchor.association)}</span> : null}
      {anchor.roles.map((role) => <Badge key={role} variant="secondary">{knowledgeLabel(role)}</Badge>)}
    </figcaption>
    <blockquote lang="en" className="whitespace-pre-wrap break-words border-l-2 pl-3 text-sm/relaxed">{anchor.quote}</blockquote>
    <p className="break-all text-xs text-muted-foreground">{anchor.passage.locator} · 字符 [{anchor.start}, {anchor.end}) · {anchor.passage.parser_version}</p>
    <details className="min-w-0" onToggle={(event) => setFullText(event.currentTarget.open)}>
      <summary className="cursor-pointer text-xs">查看完整英文段落与定位 ID</summary>
      {fullText ? <div className="mt-2 flex min-w-0 flex-col gap-2">
        <p lang="en" className="whitespace-pre-wrap break-words text-sm/relaxed">{anchor.passage.text}</p>
        <p className="break-all text-xs text-muted-foreground">段落 ID：{anchor.passage.id}{anchor.entity_ref ? ` · 原实体引用：${anchor.entity_ref}` : ""}</p>
      </div> : null}
    </details>
  </figure>;
}


export function FactFields({ fact }: { fact: Record<string, unknown> }) {
  const fields = [["relation", "关系类型"], ["drug_names", "原始药物／治疗名称字段"], ["population", "人群／疾病"], ["treatment", "治疗"], ["comparator", "比较对象"], ["conditions", "条件"], ["scope", "研究范围"]];
  return <dl className="flex min-w-0 flex-col gap-3">{fields.map(([key, label]) => {
    const value = fact[key];
    if (value === undefined || value === null || value === "") return null;
    return <div key={key} className="min-w-0"><dt className="text-xs font-medium text-muted-foreground">{label}</dt><dd className="mt-1 whitespace-pre-wrap break-words text-sm/relaxed">{typeof value === "string" ? key === "relation" ? knowledgeLabel(value) : value : Array.isArray(value) && value.every((item) => typeof item === "string") ? value.join("\n") : JSON.stringify(value, null, 2)}</dd></div>;
  })}</dl>;
}

type ResultRow = { measure: string; group: string; value: string | number };

function isResultRow(value: unknown): value is ResultRow {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const row = value as Record<string, unknown>;
  return typeof row.measure === "string" && typeof row.group === "string"
    && (typeof row.value === "string" || typeof row.value === "number");
}

export function ResultFields({ results }: { results: unknown }) {
  if (results === undefined || results === null || (Array.isArray(results) && !results.length)) return null;
  if (!Array.isArray(results) || !results.every(isResultRow)) {
    return <JsonDetails title="查看原始结局与数值" value={results} />;
  }
  return <section aria-label="原始结局与数值" className="flex min-w-0 flex-col gap-3">
    <h3 className="text-sm font-medium">结局与数值</h3>
    <div className="grid min-w-0 grid-cols-1 gap-3 xl:grid-cols-2">{results.map((row, index) => <article key={index} className="min-w-0 rounded-lg border p-3">
      <dl className="flex min-w-0 flex-col gap-3">
        <div><dt className="text-xs text-muted-foreground">指标（measure）</dt><dd lang="en" className="mt-1 whitespace-pre-wrap break-words text-sm/relaxed">{row.measure}</dd></div>
        <div><dt className="text-xs text-muted-foreground">组别（group）</dt><dd lang="en" className="mt-1 whitespace-pre-wrap break-words text-sm/relaxed">{row.group}</dd></div>
        <div><dt className="text-xs text-muted-foreground">结果（value）</dt><dd lang="en" className="mt-1 whitespace-pre-wrap break-words text-sm/relaxed">{row.value}</dd></div>
      </dl>
    </article>)}</div>
    <JsonDetails title="展开结局与数值的原始字段" value={results} />
  </section>;
}

export function KnowledgeStatusBadges({ item }: { item: Pick<KnowledgeCandidate, "ai_status" | "human_status" | "publication_status"> }) {
  return <div className="flex flex-wrap gap-2">
    <Badge variant={item.publication_status === "published" ? "default" : "outline"}>{STATES[item.publication_status ?? "draft"] ?? item.publication_status}</Badge>
    <Badge variant="outline">AI {STATES[item.ai_status ?? "pending"] ?? item.ai_status}</Badge>
    <Badge variant="outline">{STATES[item.human_status ?? "unchecked"] ?? item.human_status}</Badge>
  </div>;
}
