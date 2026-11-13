export type RegistryEntity = {
  id: string;
  key: string;
  kind: "drug" | "disease";
  name: string;
  name_en: string;
  level: string;
  status: string;
  source_status: string;
  revision: number;
  main_cancer: { id: string; name: string } | null;
  external_ids: Record<string, unknown>;
  provenance?: { is_blood_tumor?: boolean | null; source_form?: string; source_name_cn?: string; source_name_en?: string; source_aliases?: string[]; classification_basis?: string; origin?: string; [key: string]: unknown };
  mention_count: number;
  alias_count: number;
};

export type RegistryEvidence = {
  quote?: string;
  passage_id?: string;
  locator?: string;
  source_run?: string;
  group_id?: string;
  pmid?: string;
  [key: string]: unknown;
};

export type RegistryRule = {
  id: string;
  revision: number;
  kind: "drug" | "disease";
  alias: string;
  scope: { type: string; key: string };
  action: string;
  status: string;
  origin: string;
  reason: string;
  provenance?: { match_relation?: string; required_qualifiers?: unknown; [key: string]: unknown };
  canonical_entity: RegistryEntity | null;
  alternatives: { entity_key?: string; id?: string; name?: string; label?: string; [key: string]: unknown }[];
  evidence: RegistryEvidence[];
  mention_count: number;
};

export type RegistryRuleDetail = RegistryRule & { mentions: RegistryMention[]; decisions: RegistryDecision[] };

export type RegistryMention = {
  id?: string;
  candidate_id?: string;
  group_id?: string;
  source_run?: string;
  label?: string;
  original_label?: string;
  article?: { title?: string; pmid?: string; doi?: string };
  context?: Record<string, unknown>;
  anchors?: RegistryEvidence[];
  [key: string]: unknown;
};

export type RegistryDecision = {
  id: string;
  action: string;
  actor?: string | null;
  reason: string;
  created_at?: string;
  origin?: string;
  [key: string]: unknown;
};

export type RegistryEntityDetail = RegistryEntity & {
  rules: RegistryRule[];
  mentions: RegistryMention[];
  children: RegistryEntity[];
  decisions: RegistryDecision[];
};

export type RegistryPage<T> = { count: number; limit: number; offset: number; results: T[] };

export type RegistrySummary = {
  entities: { total: number; drug: number; disease: number; main: number; subtype: number; unassigned: number; seed: number; literature_new: number; candidate: number };
  rules: { total: number; confirmed: number; proposed: number; unresolved: number; rejected: number; revoked: number };
  mentions: { total: number; resolved: number; proposed: number; unresolved: number; ignored: number };
  bootstrap: { sealed: boolean; batch_id: string | null; source: Record<string, unknown> };
  can_review: boolean;
};

const LABELS: Record<string, string> = {
  drug: "药物", disease: "癌种／疾病", main: "主癌种", subtype: "亚型",
  unassigned: "层级待定", not_applicable: "不适用", active: "已登记", candidate: "候选",
  seed: "基础快照", literature_new: "文献新增", proposed: "待审核", unresolved: "待明确",
  confirmed: "已确认", rejected: "已拒绝", revoked: "已撤销", global: "全局", article: "文章范围",
  source_run: "批次范围", context: "限定上下文", document: "文献范围",
  rule: "规则", dictionary: "字典", human: "人工", user: "用户已指定", model: "模型",
  user_instruction: "用户已指定", exact_dictionary: "字典唯一匹配", local_rule: "确定性规则",
  alias: "别名对应", match: "实体对应", existing: "复用已有实体", new: "新增实体", create: "新增实体",
  same: "同一实体", same_entity: "同一实体", classify: "分类", classification: "分类",
  confirm: "确认", defer: "保留待定", reject: "拒绝", revoke: "撤销",
  set_classification: "调整分类", rename: "调整名称", activate: "确认登记",
  possible_combination: "可能是组合名称", not_reclassified: "未重新分类", pending: "待处理",
  ignore: "不归入当前实体范围", register: "登记实体",
  qualified_form: "核心疾病＋条件", exact_alias: "同一实体别名",
};

export function registryLabel(value: string) {
  return LABELS[value] ?? value;
}

export function registryQuery(values: Record<string, string | number>) {
  return new URLSearchParams(Object.entries(values).filter(([, value]) => value !== "").map(([key, value]) => [key, String(value)])).toString();
}
