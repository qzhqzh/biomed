import type { EvidencePresentation } from "@/lib/knowledge-presentation";

export type KnowledgeArticle = {
  material_id: string;
  version_id: string;
  title: string;
  pmid: string;
  pmcid: string;
  doi: string;
  sha256: string;
  source_url: string;
  rights: string;
  integrity_status: string;
};

export type KnowledgeEntity = {
  id: string;
  kind: string;
  subtype: string;
  label: string;
  status: string;
  scope_version_id: string;
  attributes: Record<string, unknown>;
  candidate_count: number;
};

export type KnowledgeCandidate = {
  id: string;
  source_run: string;
  group_id: string;
  status: string;
  article: KnowledgeArticle;
  ai_status?: string;
  human_status?: string;
  publication_status?: string;
  published_at?: string | null;
  current_review_id?: string | null;
  assessment: Record<string, unknown>;
  execution?: KnowledgeStage;
  compatibility?: KnowledgeStage;
  target_readiness?: KnowledgeStage & { reason_code: string; version: string };
  quality?: KnowledgeStage;
  eligibility?: {
    status: "eligible" | "deferred";
    reasons: string[];
    semantic_correctness?: "unassessed";
  };
  normalized_participants?: {
    entity_ref: string;
    original_label: string;
    state: string;
    canonical: {
      id: string;
      name: string;
      kind: "drug" | "disease";
      level: string;
      entity_level: string | null;
      main_cancer: { id: string; name: string } | null;
    } | null;
    qualifiers: Record<string, unknown>;
  }[];
  relationship_participants?: { entity_id: string; entity_ref: string; label: string; role: string; anchor_ids: string[] }[];
  fact_summary?: Record<string, unknown>;
  evidence_presentation?: EvidencePresentation;
  entity_count: number;
  anchor_count: number;
};

export type KnowledgeStage = {
  status: string;
  reason?: string;
  quality_dimensions?: { core_valid: boolean; target_covered: boolean; participants_usable: boolean };
  issues?: unknown[];
  [key: string]: unknown;
};

export type KnowledgeAnchor = {
  id: string;
  candidate_id: string;
  entity_id: string | null;
  entity_ref: string;
  purpose: string;
  association: string;
  roles: string[];
  start: number;
  end: number;
  quote: string;
  attributes: Record<string, unknown>;
  passage: { id: string; locator: string; parser_version: string; text: string };
};

export type KnowledgeEntityDetail = KnowledgeEntity & {
  article: KnowledgeArticle;
  candidates: KnowledgeCandidate[];
  anchors: KnowledgeAnchor[];
  anchor_count: number;
  candidates_truncated: boolean;
  anchors_truncated: boolean;
};

export type KnowledgeCandidateDetail = KnowledgeCandidate & {
  fact: Record<string, unknown>;
  provenance: Record<string, unknown>;
  entities: KnowledgeEntity[];
  anchors: KnowledgeAnchor[];
};

export type KnowledgePage<T> = { count: number; limit: number; offset: number; results: T[] };

export const KNOWLEDGE_KINDS: Record<string, string> = {
  treatment: "治疗方式",
  biomarker: "生物标志物",
  variant: "变异",
  drug: "药物",
  disease: "疾病",
  gene: "基因",
  protein: "蛋白",
};

const LABELS: Record<string, string> = {
  ...KNOWLEDGE_KINDS,
  candidate: "候选词条",
  research_candidate: "研究候选",
  category: "类别",
  modality: "治疗方式",
  regimen: "具体方案",
  dynamic_feature: "动态特征",
  signature: "特征组合",
  measurement: "检测指标",
  model_selected: "关系模型显式选择",
  exact_fact_text: "事实字段原词匹配",
  context_only: "原文提及，未建立主关系",
  definition: "同篇定义",
  mention: "实体提及",
  evidence: "关系引用原文",
  context: "上下文",
  biomarker_candidate: "生物标志物用途候选",
  treatment_outcome: "治疗与结局",
  biomarker_association: "生物标志物关联",
  intervention: "实际干预",
  comparator: "比较对象",
  molecular: "分子条件／标志物",
  mechanism: "机制关系",
  recommendation: "推荐关系",
};

export function knowledgeLabel(value: string) {
  return LABELS[value] ?? value;
}

export function candidateReviewLabel(assessment: Record<string, unknown>) {
  if (assessment.structured_common_core_usable === true) return "参考核验：核心内容通过";
  if (assessment.structured_common_core_usable === false) return "参考核验：仍有内容错误";
  return "参考核验：查看记录";
}

export function knowledgeQuery(values: Record<string, string | number>) {
  return new URLSearchParams(Object.entries(values).filter(([, value]) => value !== "").map(([key, value]) => [key, String(value)])).toString();
}
