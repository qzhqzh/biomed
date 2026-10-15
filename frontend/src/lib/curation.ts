import { useEffect, useState } from "react";

export type Release = {
  id: string;
  namespace: string;
  version: string;
  source_url: string;
  rights: string;
  count: number;
  sha256: string;
  imported_at: string;
  active: boolean;
};
export type Overview = {
  materials: number;
  versions: number;
  evidence: Record<string, number>;
  workflows: Record<string, number>;
  sources: Release[];
  worker_online: boolean;
  model_configured: boolean;
  publication_policy: string;
};
export type Concept = {
  id: number;
  identifier: string;
  label: string;
  definition: string;
  synonyms: string[];
  cross_references: string[];
  parents: string[];
};
export type Material = {
  id: string;
  title: string;
  kind: string;
  identifiers: Record<string, string>;
  metadata: Record<string, string>;
  created_at: string;
};
export type Version = {
  integrity_status: string;
  integrity_note: string;
  id: string;
  sha256: string;
  media_type: string;
  filename: string;
  source_url: string;
  rights: string;
  acquired_at: string;
};
export type Passage = {
  id: string;
  parser_version: string;
  ordinal: number;
  locator: string;
  text: string;
};
export type MaterialDetail = Material & {
  versions: Version[];
  selected_version: string | null;
  passage_total: number;
  passages: Passage[];
  parse_job: Work | null;
};
export type Work = {
  id: string;
  kind: string;
  payload: Record<string, string>;
  state: string;
  attempts: number;
  message: string;
  result: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  runs?: {
    number: number;
    outcome: string;
    message: string;
    started_at: string;
    finished_at: string | null;
  }[];
};
export type ClinicalEvidence = {
  id: string;
  assertion: string;
  quote: string;
  context: Record<string, unknown>;
  entity_links: {
    namespace: string;
    identifier: string;
    label: string;
    release: unknown;
  }[];
  ai_status: string;
  human_status: string;
  publication_status: string;
  published_at: string | null;
  source: {
    material_id: string;
    title: string;
    version_id: string;
    sha256: string;
    rights: string;
    source_url: string;
    passage_id: string;
    parser_version: string;
    locator: string;
    text: string;
  };
  reviews: {
    id: string;
    kind: string;
    decision: string;
    reason: string;
    actor: string | null;
    provenance: Record<string, string>;
    created_at: string;
  }[];
};
export type Session = { username: string | null; can_review: boolean };
export const STATES: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  succeeded: "完成",
  failed: "失败",
  blocked: "待处理",
  waiting_configuration: "待配置模型",
  published: "已发布",
  draft: "候选",
  withdrawn: "已撤回",
  unchecked: "未人工抽查",
  confirmed: "人工已核验",
  flagged: "人工发现问题",
  pending: "待审核",
  pass: "通过",
  fail: "未通过",
  confirm: "确认",
  flag: "撤回",
  interrupted: "执行中断",
  retry: "自动重试",
};
export const KINDS: Record<string, string> = {
  sync_standard: "更新标准库",
  pubmed_search: "PubMed 检索",
  acquire_pubmed: "获取文献元数据",
  acquire_pmc: "获取 PMC 全文",
  parse: "解析文本",
  extract: "抽取候选证据",
  review_ai: "AI 审核",
  paper: "文献",
  guideline: "指南",
  other: "其他文本",
};

export async function curation<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  const token = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/)?.[1];
  if (token) headers.set("X-CSRFToken", decodeURIComponent(token));
  const response = await fetch(`/api/v1/curation/${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  const data = await response.json().catch(() => null);
  if (!response.ok)
    throw new Error(
      typeof data?.error === "string"
        ? data.error
        : `请求失败（${response.status}）`,
    );
  return data as T;
}

export function useCuration<T>(path: string | null, interval = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(Boolean(path));
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setData(null);
    setError("");
    setLoading(Boolean(path));
    if (!path) return () => controller.abort();
    async function load() {
      try {
        const result = await curation<T>(path!, { signal: controller.signal });
        if (!controller.signal.aborted) {
          setData(result);
          setError("");
        }
      } catch (err) {
        if (!controller.signal.aborted)
          setError(err instanceof Error ? err.message : "读取失败");
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          if (interval) timer = setTimeout(load, interval);
        }
      }
    }
    void load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [path, revision, interval]);
  return { data, error, loading, refresh: () => setRevision((v) => v + 1) };
}

export function date(value: string) {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}
