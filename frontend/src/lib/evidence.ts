export type EvidenceKind = "guideline" | "drug" | "trial"
export type CaseFields = { cancer: string; stage: string; biomarker: string; prior_therapy: string; city: string }
export type Evidence = {
  id: string; kind: EvidenceKind; title: string; summary: string; cancer: string;
  stage: string; biomarker: string; city: string; drug_name: string;
  source_title: string; source_locator: string; source_version: string; source_text: string;
  updated_at: string; is_demo: true;
  matched?: string[]; unknown?: string[]; conflicts?: string[];
  match_state?: "excluded" | "needs_review" | "demo_match";
}
export type SearchResult = {
  run_id: string; status: "insufficient" | "matched" | "no_match";
  results: Evidence[]; excluded: Evidence[]; message: string;
}
export type Extraction = { fields: CaseFields; evidence: Partial<CaseFields>; warnings: string[] }
export type DemoCase = { id: string; text: string }
export type IssueCategory = "extraction" | "matching" | "source" | "interaction" | "other"
export type Issue = {
  id: string; title: string; category: IssueCategory; observation: string; expected: string;
  lesson: string; run_id: string | null; status: "open" | "resolved"; created_at: string; updated_at: string;
}
export const KIND_LABELS: Record<EvidenceKind, string> = { guideline: "指南", drug: "药物", trial: "临床试验" }
export const CATEGORY_LABELS: Record<IssueCategory, string> = { extraction: "字段提取", matching: "条件匹配", source: "资料覆盖与来源", interaction: "界面交互", other: "其他" }
export const EMPTY_FIELDS: CaseFields = { cancer: "", stage: "", biomarker: "", prior_therapy: "", city: "" }

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/)?.[1]
  const res = await fetch(`/api/v1/evidence/${path}/`, {
    ...init, credentials: "same-origin", cache: "no-store",
    headers: { "Content-Type": "application/json", ...(token ? { "X-CSRFToken": decodeURIComponent(token) } : {}) },
  })
  if (!res.ok) {
    const data = await res.json().catch(() => null)
    const detail = data?.detail
    const message = typeof detail === "string" ? detail : Array.isArray(detail)
      ? detail.map((item: { loc?: string[]; msg?: string }) => `${item.loc?.slice(1).join(" / ") ?? "字段"}：${item.msg ?? "无效"}`).join("；")
      : res.status === 403 ? "请求校验失败，请刷新页面后重试。" : `请求失败（${res.status}），请稍后重试。`
    throw new Error(message)
  }
  return res.json()
}
export const fetchEvidenceCatalog = (signal?: AbortSignal) => request<{ items: Evidence[] }>("catalog", { signal })
export const fetchDemoCases = (signal?: AbortSignal) => request<{ items: DemoCase[] }>("cases", { signal })
export const extractCase = (text: string) => request<Extraction>("extract", { method: "POST", body: JSON.stringify({ text }) })
export const searchEvidence = (fields: CaseFields) => request<SearchResult>("search", { method: "POST", body: JSON.stringify({ fields, confirmed: true }) })
export const fetchIssues = (signal?: AbortSignal) => request<{ items: Issue[] }>("issues", { signal })
export const createIssue = (data: Omit<Issue, "id" | "status" | "created_at" | "updated_at">) => request<Issue>("issues", { method: "POST", body: JSON.stringify(data) })
export const updateIssue = (id: string, data: Pick<Issue, "status" | "lesson">) => request<Issue>(`issues/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(data) })
export async function downloadIssues() {
  const data = await request<{ schema_version: string; items: Issue[] }>("issues/export")
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }))
  const link = document.createElement("a")
  link.href = url; link.download = "learning-log.json"; document.body.append(link); link.click(); link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
export function geneMention(item: Evidence): string | null {
  return item.biomarker.match(/^[A-Z][A-Z0-9-]+/i)?.[0] ?? null
}
export function formatDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false })
}

export function filterKind(items: Evidence[], kind: string): Evidence[] {
  return kind === "all" ? items : items.filter(item => item.kind === kind)
}
