import { ExternalLink } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorMessage, NoData } from "@/components/curation/Common";
import { date, useCuration, type Concept } from "@/lib/curation";
import { preferredChinese, TRANSLATION_STATUS } from "@/lib/standard-labels";

type SourceRef = { ref_type?: string; ref_id?: string; ref_url?: string };
type TargetComponent = {
  component_id?: number;
  accession?: string;
  component_description?: string;
  component_type?: string;
  relationship?: string;
  target_component_synonyms?: { component_synonym: string; syn_type: string }[];
};
type Mechanism = {
  id: string | number;
  mechanism_of_action: string;
  action_type: string | null;
  direct_interaction: boolean | number | null;
  disease_efficacy: boolean | number | null;
  source_url: string;
  raw_sha256: string;
  version: string;
  refs: SourceRef[];
  variant: {
    accession: string | null;
    isoform: string | null;
    mutation: string | null;
    organism: string | null;
    tax_id: number | string | null;
    version: string;
  } | null;
  mechanism_comment: string | null;
  selectivity_comment: string | null;
  binding_site_comment: string | null;
  target: {
    id: string;
    label: string;
    organism: string | null;
    target_type: string | null;
    components: TargetComponent[];
  } | null;
};
type DrugResources = {
  identifier: string;
  datasets: {
    namespace: string;
    version: string;
    source_url: string;
    rights: string | Record<string, unknown> | null;
    imported_at: string;
    counts: Record<string, number>;
  }[];
  classes: {
    id: string;
    label: string;
    translations?: Concept["translations"];
    relation: string;
    relation_source: string;
    direct: boolean;
    source_url: string;
    raw_sha256: string;
    version: string;
  }[];
  molecules: {
    id: string;
    label: string;
    parent_id: string | null;
    mapping: { method: string; status: string };
    mechanisms: Mechanism[];
  }[];
  notice: string;
};

function validUrl(value: string | undefined) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function SourceLink({ url, label }: { url?: string; label: string }) {
  const href = validUrl(url);
  return href ? (
    <a href={href} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 break-words text-primary underline">
      {label}<ExternalLink className="size-3 shrink-0" />
    </a>
  ) : <span className="break-words text-muted-foreground">{label}（未提供链接）</span>;
}

function sourceFlag(value: boolean | number | null) {
  return value === true || value === 1 ? "是" : value === false || value === 0 ? "否" : "未提供";
}

function rightsText(rights: DrugResources["datasets"][number]["rights"]) {
  if (typeof rights === "string") return rights;
  if (!rights) return "许可说明见来源";
  return [rights.name, rights.label, rights.license, rights.attribution].filter((item) => typeof item === "string").join(" · ") || "许可说明见来源";
}

function rightsUrl(rights: DrugResources["datasets"][number]["rights"]) {
  if (!rights || typeof rights === "string") return undefined;
  const url = rights.terms_url ?? rights.url ?? rights.source_url;
  return typeof url === "string" ? url : undefined;
}

function MechanismDetails({ mechanism }: { mechanism: Mechanism }) {
  const target = mechanism.target;
  return (
    <article className="flex flex-col gap-3 py-4">
      <p className="font-medium leading-relaxed">{mechanism.mechanism_of_action || "来源未提供机制描述"}</p>
      <div className="flex flex-wrap gap-2">
        <Badge variant="secondary">{mechanism.action_type || "作用类型未提供"}</Badge>
        <Badge variant="outline">ChEMBL {mechanism.version}</Badge>
      </div>
      {mechanism.variant && (
        <div className="flex flex-col gap-2">
          <Badge variant="outline">变体限定</Badge>
          <p className="font-medium break-words">{mechanism.variant.mutation || "来源未提供变体描述"}</p>
          <p className="text-xs leading-relaxed text-muted-foreground">
            蛋白 accession：{mechanism.variant.accession || "未提供"} · 异构体：{mechanism.variant.isoform || "未提供"}
          </p>
          <p className="text-xs leading-relaxed text-muted-foreground">
            {mechanism.variant.organism || "物种未提供"}{mechanism.variant.tax_id && ` · Taxonomy ${mechanism.variant.tax_id}`} · 来源版本 {mechanism.variant.version || mechanism.version}
          </p>
        </div>
      )}
      {(mechanism.mechanism_comment || mechanism.selectivity_comment || mechanism.binding_site_comment) && (
        <dl className="flex flex-col gap-2 text-xs leading-relaxed">
          {mechanism.mechanism_comment && <div><dt className="font-medium">机制补充</dt><dd className="break-words">{mechanism.mechanism_comment}</dd></div>}
          {mechanism.selectivity_comment && <div><dt className="font-medium">选择性条件</dt><dd className="break-words">{mechanism.selectivity_comment}</dd></div>}
          {mechanism.binding_site_comment && <div><dt className="font-medium">结合位点说明</dt><dd className="break-words">{mechanism.binding_site_comment}</dd></div>}
        </dl>
      )}
      {target ? (
        <div className="flex flex-col gap-2">
          <h5 className="font-medium">靶点：{target.label || target.id}</h5>
          <p className="text-xs text-muted-foreground">{target.id} · {target.organism || "物种未提供"} · {target.target_type || "靶点类型未提供"}</p>
          {target.components?.length ? (
            <ul className="flex flex-col gap-2 pl-4">
              {target.components.map((component, index) => {
                const genes = component.target_component_synonyms?.filter((item) => item.syn_type === "GENE_SYMBOL").map((item) => item.component_synonym) ?? [];
                return (
                  <li key={`${component.component_id ?? component.accession ?? "component"}:${index}`} className="list-disc break-words text-xs leading-relaxed">
                    {component.component_description || component.accession || "靶点组分"}
                    {component.accession && ` · ${component.accession}`}
                    {genes.length > 0 && ` · ${genes.join(" / ")}`}
                    {component.relationship && ` · ${component.relationship}`}
                  </li>
                );
              })}
            </ul>
          ) : <p className="text-xs text-muted-foreground">来源未提供靶点组分。</p>}
        </div>
      ) : <p className="text-muted-foreground">该机制没有明确关联的靶点记录。</p>}
      <details className="text-xs leading-relaxed">
        <summary className="cursor-pointer text-muted-foreground">原始参考与来源标记</summary>
        <div className="mt-3 flex flex-col gap-2">
          <p>直接相互作用（来源标记）：{sourceFlag(mechanism.direct_interaction)}</p>
          <p>疗效关联（来源标记）：{sourceFlag(mechanism.disease_efficacy)}；不代表本项目确认临床疗效或获批适应症。</p>
          {mechanism.refs?.length ? (
            <ul className="flex flex-col gap-2">
              {mechanism.refs.map((ref, index) => (
                <li key={`${ref.ref_type}:${ref.ref_id}:${index}`}>
                  <SourceLink url={ref.ref_url} label={[ref.ref_type, ref.ref_id].filter(Boolean).join(" · ") || `来源参考 ${index + 1}`} />
                </li>
              ))}
            </ul>
          ) : <p className="text-muted-foreground">来源未提供独立参考链接。</p>}
          <SourceLink url={mechanism.source_url} label="ChEMBL 机制原始记录" />
          <p className="break-all text-muted-foreground">原始数据 SHA-256：{mechanism.raw_sha256 || "未提供"}</p>
        </div>
      </details>
    </article>
  );
}

export function DrugResourceDetails({ identifier, releaseId }: { identifier: string; releaseId: string }) {
  const { data, error, loading, refresh } = useCuration<DrugResources>(
    identifier.startsWith("RXNORM:") && releaseId
      ? `drug-resources/?identifier=${encodeURIComponent(identifier)}&release=${encodeURIComponent(releaseId)}`
      : null,
  );
  return (
    <section aria-label="药理分类与机制" className="flex flex-col gap-5">
      <Separator />
      <h3 className="font-semibold">药理分类、靶点与机制</h3>
      {loading && (
        <div role="status" className="flex flex-col gap-2">
          <span className="text-muted-foreground">正在读取该成分的扩展资源…</span>
          <Skeleton className="h-5 w-3/4" /><Skeleton className="h-5 w-full" />
        </div>
      )}
      {error && (
        <div className="flex flex-col items-start gap-3">
          <ErrorMessage error={error} />
          <Button type="button" variant="outline" onClick={refresh}>重试扩展资源</Button>
        </div>
      )}
      {!loading && !error && !data && <p className="text-muted-foreground">扩展资源暂不可用。</p>}
      {data && (
        <>
          {data.notice && <Alert><AlertDescription>{data.notice}</AlertDescription></Alert>}
          <section aria-label="EPC药理分类" className="flex flex-col gap-3">
            <h4 className="font-medium">FDA EPC 药理分类</h4>
            {data.classes.length ? (
              <div className="flex flex-col gap-4">
                {data.classes.map((item) => {
                  const chinese = preferredChinese(item);
                  return (
                  <div key={`${item.id}:${item.version}`} className="flex flex-col gap-2">
                    <p className="font-medium">{chinese?.label ?? item.label}</p>
                    {chinese && <p className="text-xs text-muted-foreground">{item.label}</p>}
                    <div className="flex flex-wrap gap-2">
                      <Badge variant="outline">{item.id}</Badge>
                      <Badge variant="secondary">{item.direct ? "直接关联" : "间接关联"}</Badge>
                      {chinese && <Badge variant="outline">{TRANSLATION_STATUS[chinese.status]}</Badge>}
                    </div>
                    <p className="text-xs text-muted-foreground">{item.relation_source} · {item.version} · {item.relation}</p>
                    <details className="text-xs">
                      <summary className="cursor-pointer text-muted-foreground">分类关联来源</summary>
                      <div className="mt-2 flex flex-col gap-2">
                        <SourceLink url={item.source_url} label="RxClass 原始关联" />
                        <p className="break-all text-muted-foreground">SHA-256：{item.raw_sha256}</p>
                      </div>
                    </details>
                  </div>
                  );
                })}
              </div>
            ) : <p className="text-muted-foreground">{data.datasets.some((dataset) => dataset.namespace === "RXCLASS_EPC") ? "已导入的完整 RxClass 快照未提供该成分的 EPC 直接关联。" : "RxClass 完整快照尚未导入；采集与校验完成后显示药理分类。"}</p>}
          </section>
          <Separator />
          <section aria-label="ChEMBL靶点与机制" className="flex flex-col gap-3">
            <h4 className="font-medium">ChEMBL 靶点与机制</h4>
            {data.molecules.length ? data.molecules.map((molecule) => (
              <article key={molecule.id} className="flex flex-col gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <h4 className="font-medium">{molecule.label || molecule.id}</h4>
                  <Badge variant="outline">{molecule.id}</Badge>
                  <Badge variant="secondary">{molecule.mapping.status === "confirmed" ? "关联已核验" : "关联候选 · 未独立核验"}</Badge>
                </div>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  匹配方式：{molecule.mapping.method === "unique_exact_name" ? "名称或来源同义词唯一精确匹配（仅去除首尾空白、忽略大小写）" : molecule.mapping.method}。{molecule.mapping.status !== "confirmed" && "尚未独立确认与该 RxNorm 成分为同一实体。"}
                </p>
                {molecule.parent_id && molecule.parent_id !== molecule.id && <p className="text-xs text-muted-foreground">分子母体：{molecule.parent_id}；盐型与母体保持独立标识。</p>}
                {molecule.mechanisms.length ? (
                  <div className="divide-y">{molecule.mechanisms.map((mechanism) => <MechanismDetails key={mechanism.id} mechanism={mechanism} />)}</div>
                ) : <p className="text-muted-foreground">当前快照没有该分子的机制记录；不表示没有药理作用。</p>}
              </article>
            )) : <NoData title="尚无关联的 ChEMBL 分子" description="当前快照未找到可展示的分子关联；不表示该成分没有已知靶点或机制。" />}
          </section>
          {data.datasets.length > 0 && (
            <details className="text-xs leading-relaxed">
              <summary className="cursor-pointer font-medium">资源版本与许可（{data.datasets.length}）</summary>
              <div className="mt-3 flex flex-col gap-4">
                {data.datasets.map((dataset) => (
                  <div key={`${dataset.namespace}:${dataset.version}`} className="flex flex-col gap-1">
                    <p className="font-medium">{dataset.namespace} · {dataset.version}</p>
                    <p>收录时间：{date(dataset.imported_at)}</p>
                    <p className="break-words text-muted-foreground">{rightsText(dataset.rights)}</p>
                    {rightsUrl(dataset.rights) && <SourceLink url={rightsUrl(dataset.rights)} label="许可与使用条款" />}
                    <SourceLink url={dataset.source_url} label="资源来源与版本" />
                  </div>
                ))}
              </div>
            </details>
          )}
        </>
      )}
    </section>
  );
}
