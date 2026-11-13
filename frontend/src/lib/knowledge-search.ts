import type { KnowledgeArticle } from "@/lib/knowledge-candidates";
import type { EvidencePresentation } from "@/lib/knowledge-presentation";

export type KnowledgeFilters = { disease: string; drug: string; biomarker: string; treatment: string };
export type KnowledgeSupport = {
  passage_id: string;
  start: number;
  end: number;
  quote: string;
  fields?: string[];
  passage?: { id: string; version_id: string; parser_version: string; locator: string; text: string; text_sha256: string };
};
export type KnowledgeLink = {
  entity_id: string;
  entity_ref: string;
  role: string;
  label: string;
  context?: string;
  aliases: { term: string; basis: string }[];
  support: KnowledgeSupport[];
};
export type KnowledgeReview = {
  id: string;
  kind: string;
  decision: string;
  reason: string;
  provider: string;
  model: string;
  policy_version: string;
  review_run: string;
  created_at: string;
  actor: string | null;
  input_sha256: string;
  result?: Record<string, unknown>;
};
export type PublishedKnowledge = {
  id: string;
  source_run: string;
  group_id: string;
  status: string;
  ai_status: string;
  human_status: string;
  publication_status: string;
  published_at: string | null;
  article: KnowledgeArticle;
  fact: Record<string, unknown>;
  evidence_presentation?: EvidencePresentation;
  review: KnowledgeReview | null;
  approved_links: KnowledgeLink[];
  matched_terms: { entity_id: string; role: string; term: string; basis: string }[];
  supporting_quotes: KnowledgeSupport[];
  review_history?: KnowledgeReview[];
};

export const KNOWLEDGE_EXAMPLE: KnowledgeFilters = {
  disease: "胆道癌", drug: "durvalumab", biomarker: "CRP", treatment: "",
};

export const KNOWLEDGE_FILTERS: { key: keyof KnowledgeFilters; label: string; example: string }[] = [
  { key: "disease", label: "癌种", example: "胆道癌" },
  { key: "drug", label: "具体药物", example: "durvalumab" },
  { key: "biomarker", label: "生物标志物", example: "CRP" },
  { key: "treatment", label: "治疗方式（可选）", example: "例如 ICI" },
];
