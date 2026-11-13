export type ResearchStage = {
  id: string;
  label: string;
  status: string;
  note: string;
};

export type ResearchSummary = {
  id: string;
  title: string;
  date: string;
  stage: string;
  status: string;
  sample_count: number;
  sample_unit?: string;
  summary: string;
};

export type ResearchIndex = {
  schema_version: string;
  current_task: {
    id: string;
    title: string;
    status: string;
    categories: string[];
    note: string;
  };
  stages: ResearchStage[];
  experiments: ResearchSummary[];
  default_experiment: string;
};

export type ResearchMetric = {
  label: string;
  numerator?: number;
  denominator?: number;
  value?: number | string;
  unit?: string;
  note: string;
};

export type ResearchQuote = {
  field: string;
  text: string;
  start: number;
  end: number;
};

export type ResearchLabel = {
  axis: string;
  id: string;
  label: string;
  role?: string;
  matches: ResearchQuote[];
};

export type ResearchCase = {
  cohort: string;
  rank: number;
  pmid: string;
  title: string;
  source_scope: string;
  reference_state: string;
  reference_relevance: string | null;
  reference_scope: string | null;
  reference_notes: string;
  reference_quotes: ResearchQuote[];
  reference_labels: ResearchLabel[];
  scope_eligible: boolean;
  rule_scope: string | null;
  rule_labels: ResearchLabel[];
  model_scope: string | null;
  model_relevance: string | null;
  model_raw_relevance?: string | null;
  model_status: string;
  model_error: string;
  model_seconds: number | null;
  model_labels: ResearchLabel[];
  rule_outcome: string;
  model_outcome: string;
  model_relevance_outcome: string;
  acceptable_relevance: string[];
};

export type ResearchKnowledgeQuality = {
  core_valid: boolean;
  target_covered: boolean;
  participants_usable: boolean;
};

export function researchKnowledgeQualityArms(preview: Record<string, unknown>): {
  label: string; quality: ResearchKnowledgeQuality;
}[] {
  return Object.entries(preview).flatMap(([label, arm]) => {
    if (!arm || typeof arm !== "object" || !("审核" in arm)) return [];
    const quality = arm["审核"];
    if (!quality || typeof quality !== "object"
      || !("core_valid" in quality) || typeof quality.core_valid !== "boolean"
      || !("target_covered" in quality) || typeof quality.target_covered !== "boolean"
      || !("participants_usable" in quality) || typeof quality.participants_usable !== "boolean") return [];
    return [{ label, quality: {
      core_valid: quality.core_valid,
      target_covered: quality.target_covered,
      participants_usable: quality.participants_usable,
    } }];
  });
}

export type ResearchKnowledgeCandidate = {
  id: string;
  stored_candidate_id?: string;
  published_knowledge_id?: string;
  pmid: string;
  title: string;
  statement: string | null;
  status: string;
  review_note: string;
  study_scope: string;
  source_role: string;
  entities: {
    id: string;
    type: string;
    text: string;
    normalization_status: string;
    standard_ids: string[];
  }[];
  quotes: {
    passage_id: string;
    locator: string;
    quote: string;
    fields: string[];
    start: number | null;
    end: number | null;
  }[];
  mapping: { target?: "biomed"; status: string; blockers: string[]; preview: Record<string, unknown> };
};

export type ResearchExperiment = ResearchSummary & {
  schema_version: string;
  task_kind?: "three_class" | "passage_screening" | "knowledge_candidates" | "target_compatibility" | "entity_resolution";
  entity_resolution?: { cases: EntityResolutionCase[]; scoring?: string; previous_record?: string };
  target_compatibility?: TargetCompatibilityPreview;
  classification_definition?: { categories: Record<string, string>; boundary_notes: string[] };
  rules_explanation?: {
    taxonomy_version: string;
    total_rules: number;
    engine_notes: string[];
    main_scope_rules: {
      id: string;
      label: string;
      effective_pattern?: string;
      pattern: string;
      fields: string[];
      case_sensitive: boolean;
      requires_context: string | null;
      exclusions: { id: string; reason: string; pattern: string }[];
    }[];
    examples: { pmid: string; note: string }[];
  };
  task: {
    question: string;
    input: string;
    outputs: string[];
    not_measured: string[];
  };
  dataset: { label: string; role: string; notes: string[]; document_count?: number };
  reference: { label: string; status: string; note: string };
  methods: {
    id: string;
    label: string;
    version: string;
    metrics: ResearchMetric[];
    conclusion: string;
  }[];
  findings: string[];
  next_steps: string[];
  identity: {
    task_version: string;
    sample_versions: string[];
    method_versions: string[];
    reference_versions: string[];
    completeness: "frozen" | "documented_only";
  };
  fingerprint: string;
  provenance: { label: string; artifact: string; sha256: string }[];
  cases?: ResearchCase[];
  knowledge_candidates?: ResearchKnowledgeCandidate[];
  comparison?: ModelComparison;
};

export type EntityResolutionCase = {
  id: string; kind: string; label: string; cohort: string;
  reference_note: string; reference_decision: string; reference_entity: string;
  reference_main?: string;
  required_qualifiers: string[]; quotes: string[];
  methods: { id: string; label: string; decision: string; entity: string; correct: boolean; seconds: number | null; reason: string;
    core_result?: { direct: boolean; recovered: boolean; accepted: boolean; explanation_only?: boolean };
    completeness?: { label: string; correct: boolean }[];
  }[];
};

export type TargetCompatibilityRecord = {
  id: string;
  group_id: string;
  source_run: string;
  candidate_id?: string | null;
  article: { title: string; pmid: string; doi: string };
  fact: Record<string, unknown>;
  compatibility_status: string;
  quality: { status: string; reason?: string; quality_dimensions?: ResearchKnowledgeQuality };
  offline_ready: boolean;
  entity_mappings: {
    entity_ref: string; kind: string; label: string; roles: string[]; status: string;
    target_id?: string; target_name?: string; target_name_cn?: string; basis?: string;
    accepted_by_exporter?: boolean;
    candidates?: { id?: string; target_id?: string; name?: string; name_cn?: string; name_en?: string }[];
  }[];
  trigger_scope: Record<string, unknown>;
  treatment_scope: Record<string, unknown>;
  target_payload: Record<string, unknown>;
  mapping_issues: { code: string; path: string; detail: string }[];
  contract_validation: Record<string, unknown>;
  matching_validation: unknown;
  live_sample_matching: string;
  review_points: string[];
  quotes: { passage_id: string; locator: string; start: number; end: number; quote: string }[];
};

export type TargetCompatibilityPreview = {
  preview_only: true;
  batch_labels?: Record<string, string>;
  target_written: false;
  dictionary: { snapshot_id: string; captured_at: string; counts: Record<string, number> };
  records: TargetCompatibilityRecord[];
};

type ModelClassificationScore = {
  id: string;
  label: string;
  matched: number;
  denominator: number;
  macro_recall: number | null;
  by_class: Record<string, { matched: number; denominator: number }>;
  failed: number;
  abstained: number;
  tumor_false_exclusions: number;
};

export type ModelComparison = {
  notes: string[];
  groups?: Record<string, {
    label: string;
    sample_count: number;
    denominator: number;
    unscored: number;
    reference_by_class: Record<string, number>;
    methods: ModelClassificationScore[];
  }>;
  pairwise: {
    question: string;
    before: string;
    after: string;
    parameter_ratio: number;
    accuracy_delta_pp: number | null;
    macro_recall_delta_pp: number | null;
    correct_delta: number;
    improved: number;
    regressed: number;
    time_ratio: number;
    memory_ratio: number;
  }[];
  methods: (ModelClassificationScore & {
    parameters: number;
    precision: string;
    mean_seconds: number;
    median_seconds: number;
    p95_seconds: number;
    peak_allocated_mib: number;
    peak_reserved_mib: number;
    nvml_process_peak_mib: number;
  })[];
  cases: {
    group?: string;
    pmid: string;
    title: string;
    reference: string | null;
    reference_notes: string;
    reference_quotes: ResearchQuote[];
    predictions: {
      method_id: string;
      label: string;
      category: string | null;
      status: string;
      outcome: string;
      seconds: number;
      raw_output: string;
    }[];
  }[];
};

const LABELS: Record<string, string> = {
  solid_candidate: "实体瘤候选",
  solid_boundary_candidate: "实体瘤边界候选",
  precursor_candidate: "癌前病变候选",
  benign_candidate: "良性病变候选",
  hematologic_candidate: "血液肿瘤候选",
  uncertain: "待判定 / 未分型",
  solid: "实体瘤",
  solid_boundary: "实体瘤边界病变",
  hematologic: "血液肿瘤",
  mixed: "混合 / 多类别",
  oncology_unspecified: "肿瘤相关，未分型",
  non_neoplastic: "非肿瘤",
  benign: "良性病变",
  precursor: "癌前病变",
  insufficient: "信息不足",
  not_oncology: "非肿瘤相关",
  solid_malignancy: "实体恶性肿瘤",
  hematologic_malignancy: "血液恶性肿瘤",
  agreed: "与参考一致",
  matched: "与参考一致",
  classified: "完成分类",
  disagreed: "与参考不一致",
  abstained: "未完成分类",
  failed: "输出校验失败",
  unscored: "未评分",
  not_run: "尚未运行",
  validated_candidate: "输出校验通过",
  invalid_output: "输出校验失败",
  unresolved: "参考未能确定",
  determinate: "参考可判定",
  indeterminate: "参考仍待判定",
  ambiguous: "参考仍待判定",
  parsed_fulltext: "已有解析全文",
  abstract_only: "仅摘要",
  completed: "已完成",
  measured: "已测量",
  evaluated: "已评估",
  planned: "计划中 · 尚未测试",
  waiting_configuration: "待配置",
  documented_only: "仅有历史记录",
  frozen: "已冻结版本",
  development_pipeline: "开发样本",
  development: "开发样本",
  development_and_historical_holdout: "开发样本与历史保留样本",
  assistant_reviewed_provisional: "助手暂定参考",
  model_reference_not_human_gold: "模型参考，非人工金标准",
  not_configured: "尚未配置",
  site: "癌种 / 部位",
  histology: "组织学类型",
  boundary: "病变边界",
  population: "研究对象",
  study_type: "研究设计",
  topic: "主题",
  publication_status: "发表状态",
  study_subject: "研究对象",
  background: "背景提及",
  title: "标题",
  abstract: "摘要",
  mesh: "MeSH",
  publication_types: "发表类型",
  supported: "原文支持",
  needs_revision: "需修正后再核验",
  rejected: "核验拒绝",
  partially_supported: "部分支持，需修正",
  unsupported: "原文不支持",
  blocked: "映射受阻",
  preview_only: "仅映射预览",
  compatible_preview: "结构可映射，尚未导入",
  exact_match: "精确匹配",
  exact_alias_unique: "唯一精确别名匹配",
  ambiguous_match: "匹配不唯一",
  unanchored: "实体未定位到原文",
  invalid_entity: "实体类型不合法",
  not_applicable: "不适用",
  human: "人体研究",
  animal: "动物研究",
  cell: "细胞研究",
  current_study: "本文研究",
  cited_study: "引述其他研究",
  recommendation_or_regulatory_report: "文中转述建议或监管信息",
  unclear: "未确定",
  disease: "疾病",
  drug: "药物",
  gene: "基因",
  protein: "蛋白",
  variant: "变异",
  biomarker: "生物标志物",
  treatment: "治疗方式",
  statement: "知识陈述",
  entities: "实体",
  disease_ids: "疾病实体",
  intervention_ids: "干预实体",
  comparator_ids: "对照实体",
  molecular_ids: "分子实体",
  molecular_logic: "分子条件逻辑",
  regimen: "治疗方案",
  comparison: "对照条件",
  outcome: "结果",
  direction: "效应方向",
  study_scope: "研究层级",
  source_role: "来源角色",
  conditions: "适用条件",
  limitations: "局限",
};

export function researchLabel(value: string | null | undefined): string {
  return value ? LABELS[value] ?? value : "未提供";
}

export function researchNormalizationLabel(value: string): string {
  if (value === "ambiguous") return "多义匹配，待消歧";
  if (value === "unresolved") return "未匹配标准实体";
  return researchLabel(value);
}

export function cohortLabel(value: string): string {
  if (value === "pilot30") return "30 篇测试集";
  if (value.endsWith("first20")) return "原始 20 篇";
  if (value.endsWith("solid10")) return "实体瘤补充 10 篇";
  return value;
}
