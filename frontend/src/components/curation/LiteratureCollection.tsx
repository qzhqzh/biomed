import { useState, type FormEvent } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardFooter,
} from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { ErrorMessage } from "@/components/curation/Common";
import { curation, useCuration, date } from "@/lib/curation";
import { useUnsavedChanges } from "@/lib/navigation";

type Limits = {
  max_items: number;
  max_fulltexts: number;
  max_requests: number;
  max_bytes: number;
  interval_seconds: number;
  bytes_per_second: number;
  page_size: number;
};
type Label = {
  axis: string;
  id: string;
  label: string;
  matches: { field: string; text: string }[];
};
type Collection = {
  id: string;
  state: string;
  phase: string;
  start_date: string;
  end_date: string;
  limits: Limits;
  estimate: {
    candidate_count?: number;
    observed_at?: string;
    acquisition?: {
      mode: string;
      recorded_at: string;
      reference_count: number;
      reference_count_observed_at: string;
      baseline_started_at: string;
      baseline_completed_at: string | null;
      incremental_replay_from: string;
      incremental_watermark: string | null;
    };
  };
  message: string;
  recovery?: {
    consecutive_failures: number;
    max_failures: number;
    waiting: boolean;
    next_attempt_at: string | null;
    retry_not_before: string | null;
    open_alerts: number;
  };
  saved: number;
  reused: number;
  discovered: number;
  active_seconds: number;
  taxonomy_version: string;
  classification_taxonomy_version: string;
  classification_taxonomy_sha256: string;
  classification_statistics_loaded?: boolean;
  statistics_as_of?: string;
  classification_statistics_saved?: number;
  counts_as_of?: string;
  metrics_as_of?: string;
  classification_revisions: {
    taxonomy_version: string;
    taxonomy_sha256: string;
    count: number;
  }[];
  fulltexts: Record<string, number>;
  scopes: Record<string, number>;
  metrics: {
    requests: number;
    wire_bytes: number;
    decoded_bytes: number;
    charged_bytes: number;
    errors: number;
    reservations: number;
  };
  next_job: { state: string; available_at: string } | null;
  entries?: {
    pmid: string;
    material_id: string | null;
    title: string;
    fulltext_state: string;
    message: string;
    classification: {
      scope: string;
      labels: Label[];
      publication_status_conflicts?: Label[];
    } | null;
  }[];
  entries_truncated?: boolean;
};
const STATES: Record<string, string> = {
  estimating: "查询规模中",
  ready: "待开始",
  running: "采集中",
  paused: "已暂停",
  complete: "快照已完成",
};
const SCOPES: Record<string, string> = {
  solid_candidate: "实体瘤候选",
  mixed_candidate: "实体 / 血液混合线索",
  hematologic_candidate: "血液肿瘤线索",
  solid_boundary_candidate: "实体瘤及良性 / 癌前线索",
  benign_candidate: "良性病变线索",
  precursor_candidate: "息肉 / 癌前病变线索",
  uncertain: "待判定",
};
const FULLTEXTS: Record<string, string> = {
  pending: "未尝试全文",
  parsed: "全文已解析",
  reused: "复用已有全文",
  unavailable: "未取得 / 未解析全文",
  identity_conflict: "标识待核对",
};
const BUDGET_FIELDS = [
  ["max_items", "累计文献上限（篇）", 1, 2000000, 1],
  ["max_fulltexts", "累计全文尝试上限（篇）", 0, 2000, 1],
  ["max_requests", "累计请求上限（次）", 1, 100000, 1],
  ["max_bytes", "累计响应预算（MiB）", 1, 65536, 1024 * 1024],
  ["interval_seconds", "请求间隔（秒）", 1, 60, 1],
  ["bytes_per_second", "读取速率上限（KiB/s）", 16, 1024, 1024],
] as const;
const mib = (bytes: number) => (bytes / 1024 / 1024).toFixed(2);
const collectionDate = (value: string) =>
  new Date(value).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
  });

function BudgetFields({
  value,
  onChange,
  disabled = false,
}: {
  value: Limits;
  onChange: (value: Limits) => void;
  disabled?: boolean;
}) {
  return (
    <FieldGroup className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {BUDGET_FIELDS.map(([key, label, min, max, factor]) => (
        <Field key={key} data-disabled={disabled}>
          <FieldLabel htmlFor={`collection-${key}`}>{label}</FieldLabel>
          <Input
            id={`collection-${key}`}
            type="number"
            min={min}
            max={max}
            step={1}
            required
            disabled={disabled}
            value={value[key] / factor}
            onChange={(event) =>
              onChange({ ...value, [key]: Number(event.target.value) * factor })
            }
          />
        </Field>
      ))}
    </FieldGroup>
  );
}

function Controls({ run, refresh }: { run: Collection; refresh: () => void }) {
  const [limits, setLimits] = useState(run.limits);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = ["estimating", "running"].includes(run.state);
  useUnsavedChanges(JSON.stringify(limits) !== JSON.stringify(run.limits) || busy, "采集预算还有未提交的修改。");
  async function action(value: "pause" | "resume") {
    setBusy(true);
    setError("");
    try {
      await curation(`collections/${run.id}/`, {
        method: "POST",
        body: JSON.stringify({
          action: value,
          ...(value === "resume" ? { limits } : {}),
        }),
      });
      refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        void action("resume");
      }}
    >
      <BudgetFields
        value={limits}
        onChange={setLimits}
        disabled={busy || active || run.state === "complete"}
      />
      <p className="text-xs text-muted-foreground">
        预算为本批累计值，续跑不会清零。单路请求；流量预算按压缩响应和解压后大小的较大值扣减。暂停允许当前单次响应完成。
      </p>
      <div className="flex flex-wrap gap-2">
        {active ? (
          <Button
            type="button"
            variant="outline"
            disabled={busy}
            onClick={() => void action("pause")}
          >
            暂停采集
          </Button>
        ) : run.state !== "complete" ? (
          <Button disabled={busy}>
            {run.state === "ready" ? "按本批预算开始" : "保存预算并续跑"}
          </Button>
        ) : null}
        <Button type="button" variant="outline" onClick={refresh}>
          刷新进度
        </Button>
        <Button type="button" variant="ghost" asChild>
          <a
            href={`/api/v1/curation/collections/${run.id}/`}
            target="_blank"
            rel="noreferrer"
          >
            查看批次记录
          </a>
        </Button>
      </div>
      <ErrorMessage error={error} />
    </form>
  );
}

function NewCollection({ onCreated }: { onCreated: (id: string) => void }) {
  const [end, setEnd] = useState(() => new Date().toISOString().slice(0, 10));
  const [start, setStart] = useState(
    () => `${Number(end.slice(0, 4)) - 5}${end.slice(4)}`,
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [initialRange, setInitialRange] = useState({ start, end });
  useUnsavedChanges(start !== initialRange.start || end !== initialRange.end || busy, "新建采集批次的日期范围尚未提交。");
  async function estimate(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const created = await curation<Collection>("collections/", {
        method: "POST",
        body: JSON.stringify({ start_date: start, end_date: end }),
      });
      setInitialRange({ start, end });
      onCreated(created.id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={estimate} className="flex flex-col gap-3">
      <FieldGroup className="grid gap-3 sm:grid-cols-2">
        <Field>
          <FieldLabel htmlFor="collection-start">发表日期起点</FieldLabel>
          <Input
            id="collection-start"
            type="date"
            required
            value={start}
            onChange={(event) => setStart(event.target.value)}
          />
        </Field>
        <Field>
          <FieldLabel htmlFor="collection-end">发表日期终点</FieldLabel>
          <Input
            id="collection-end"
            type="date"
            required
            value={end}
            onChange={(event) => setEnd(event.target.value)}
          />
        </Field>
      </FieldGroup>
      <p className="text-xs text-muted-foreground">
        默认近五年，可改为更早日期做历史回填。先查询候选规模；新批次默认 50
        篇元数据、5 篇全文尝试、100 MiB 响应预算。
      </p>
      <div>
        <Button disabled={busy}>仅估算规模</Button>
      </div>
      <ErrorMessage error={error} />
    </form>
  );
}

export function LiteratureCollectionPanel() {
  const [selected, setSelected] = useState<string | null>(null);
  const [entriesExpanded, setEntriesExpanded] = useState(false);
  const [revision, setRevision] = useState<{
    runId: string;
    sha: string;
  } | null>(null);
  const list = useCuration<{ results: Collection[] }>("collections/?summary=1", 15000);
  const selectedId = selected ?? list.data?.results[0]?.id;
  const summary = list.data?.results.find((run) => run.id === selectedId);
  const revisionQuery =
    revision && revision.runId === selectedId
      ? `?classification_sha=${revision.sha}`
      : "";
  const detail = useCuration<Collection>(
    selectedId && entriesExpanded ? `collections/${selectedId}/${revisionQuery}` : null,
  );
  const run = summary;
  const statistics = detail.data?.id === selectedId ? detail.data : null;
  const taxonomyVersion =
    statistics?.classification_taxonomy_version ?? run?.taxonomy_version;
  const acquisition = run?.estimate.acquisition;
  const allCandidates = acquisition?.mode === "all_candidates";
  const referenceCount = acquisition?.reference_count ?? run?.estimate.candidate_count;
  function refresh() {
    list.refresh();
    detail.refresh();
  }
  return (
    <div className="flex flex-col gap-4" aria-label="实体瘤批量采集">
      <Card>
        <CardHeader>
          <CardTitle>实体瘤文献 · 分批采集</CardTitle>
          <CardDescription>
            先估算规模，再按预算采集；每批停止后查看实际流量、全文可用性和分类结果。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <ErrorMessage error={list.error || detail.error} />
          {run ? (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="secondary">
                  {STATES[run.state] ?? run.state}
                </Badge>
                <span>
                  {run.start_date} — {run.end_date}
                </span>
                {allCandidates && <Badge variant="outline">范围内全部候选</Badge>}
              </div>
              <dl className="grid grid-cols-2 gap-4 lg:grid-cols-4">
                {[
                  [
                    "候选检索规模",
                    referenceCount?.toLocaleString() ?? "待查询",
                  ],
                  [
                    allCandidates ? "已入库 / 候选估算" : "已入库 / 本批上限",
                    `${run.saved} / ${allCandidates ? referenceCount : run.limits.max_items}`,
                  ],
                  ["实际响应流量", `${mib(run.metrics.wire_bytes)} MiB`],
                  [
                    "可用全文",
                    (run.fulltexts.parsed ?? 0) + (run.fulltexts.reused ?? 0),
                  ],
                  [
                    "请求 / 失败",
                    `${run.metrics.requests} / ${run.metrics.errors}`,
                  ],
                  [
                    "预算已计入",
                    `${mib(run.metrics.charged_bytes)} / ${mib(run.limits.max_bytes)} MiB`,
                  ],
                  ["复用已有元数据", run.reused],
                  ["步骤累计耗时", `${run.active_seconds.toFixed(1)} 秒`],
                ].map(([label, value]) => (
                  <div key={String(label)}>
                    <dt className="text-muted-foreground">{label}</dt>
                    <dd className="mt-1 text-base font-medium tabular-nums">
                      {value}
                    </dd>
                  </div>
                ))}
              </dl>
              <p role="status">
                {run.message || "按顺序采集并保存断点，离开页面不会丢失进度。"}
              </p>
              <p className="text-sm text-muted-foreground">
                临时错误最多连续尝试 6 次，依次等待 1、5、15、30、60 分钟；来源要求更久时遵守其等待时间。
                成功保存断点后清零连续失败次数。连续失败达到上限时暂停，并生成站内及邮件告警。
              </p>
              {run.recovery && run.recovery.consecutive_failures > 0 && (
                <p role="status" className="text-sm font-medium">
                  连续失败 {run.recovery.consecutive_failures}/{run.recovery.max_failures} 次。
                  {run.recovery.waiting && run.recovery.next_attempt_at
                    ? `等待自动重试：${collectionDate(run.recovery.next_attempt_at)}（北京时间）。无需手动续跑。`
                    : run.state === "paused" ? "已暂停，断点已保存。" : "正在从原断点尝试恢复。"}
                </p>
              )}
              {allCandidates && acquisition && (
                <div className="flex flex-col gap-1 text-sm text-muted-foreground">
                  <p>
                    计数快照：{collectionDate(acquisition.reference_count_observed_at)}；全量目标记录：{collectionDate(acquisition.recorded_at)}（北京时间）。
                    候选数量会随来源更新变化，以日期范围处理完毕为完成依据。
                  </p>
                  <p>
                    {acquisition.baseline_completed_at
                      ? `元数据扫描完成于 ${collectionDate(acquisition.baseline_completed_at)}（北京时间）。`
                      : "元数据全量扫描尚未完成。"}
                    增量回查起点：{collectionDate(acquisition.incremental_replay_from)}（北京时间）；增量更新尚未执行。
                    下方数量和流量用于运行保护，达到后会暂停并保留断点。
                  </p>
                </div>
              )}
              {run.metrics.reservations > 0 && (
                <p className="text-muted-foreground">
                  有 {run.metrics.reservations}{" "}
                  次请求尚未结算，已预留预算；中断请求继续按保守预算计入。
                </p>
              )}
              {run.next_job?.state === "queued" &&
                new Date(run.next_job.available_at).getTime() > Date.now() && (
                  <p>下次尝试不早于 {date(run.next_job.available_at)}</p>
                )}
              <Controls
                key={`${run.id}-${JSON.stringify(run.limits)}`}
                run={run}
                refresh={refresh}
              />
              <p className="text-xs text-muted-foreground">
                运行状态每 15 秒更新，数量与流量统计最多延迟 60 秒。
                {run.counts_as_of && ` 数量截至 ${collectionDate(run.counts_as_of)}（北京时间）。`}
                {run.metrics_as_of && ` 流量截至 ${collectionDate(run.metrics_as_of)}（北京时间）。`}
                分类统计与文献明细在下方展开时读取。候选规模包含待判定及血液肿瘤边界文章，不是实体瘤净数量。新解析的合法全文由独立知识服务继续处理。
              </p>
            </>
          ) : (
            !list.loading && (
              <NewCollection
                onCreated={(id) => {
                  setSelected(id);
                  refresh();
                }}
              />
            )
          )}
        </CardContent>
        <CardFooter className="flex-wrap gap-2">
          <Badge variant="outline">单路 · 可恢复</Badge>
          <span className="text-muted-foreground">
            词表 {taxonomyVersion ?? "当前版本"}
          </span>
          <a
            href={`/api/v1/curation/literature-taxonomy/${taxonomyVersion ? `?version=${taxonomyVersion}` : ""}`}
            className="underline"
            target="_blank"
            rel="noreferrer"
          >
            查看分类词表
          </a>
        </CardFooter>
      </Card>
      {run && (
        <details
          className="rounded-lg border bg-background p-4"
          open={entriesExpanded}
          onToggle={(event) => setEntriesExpanded(event.currentTarget.open)}
        >
          <summary className="cursor-pointer font-medium">
            查看分类统计与本批文献{statistics?.entries ? `（${statistics.entries.length} 篇明细）` : ""}
          </summary>
          {entriesExpanded && <div className="mt-4 flex flex-col gap-4">
            {detail.loading && <p role="status">正在按需读取分类统计与文献明细…</p>}
            <ErrorMessage error={detail.error} />
            {statistics && <>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-xs text-muted-foreground">分类统计快照：{statistics.classification_statistics_saved ?? statistics.saved} 篇{statistics.statistics_as_of ? ` · ${collectionDate(statistics.statistics_as_of)}（北京时间）` : ""}。仅主动刷新时更新。</p>
                <Button type="button" variant="outline" size="sm" onClick={detail.refresh}>刷新分类与明细</Button>
              </div>
              <div className="flex flex-wrap gap-2">{Object.entries(statistics.scopes).map(([scope, count]) => <Badge key={scope} variant="outline">{SCOPES[scope] ?? scope} {count}</Badge>)}</div>
              {(statistics.classification_revisions?.length ?? 0) > 1 && <div className="flex flex-wrap items-center gap-2" aria-label="分类版本">
                <span className="text-sm text-muted-foreground">查看分类版本</span>
                {statistics.classification_revisions.map((item) => <Button key={item.taxonomy_sha256} type="button" size="sm"
                  variant={item.taxonomy_sha256 === statistics.classification_taxonomy_sha256 ? "secondary" : "outline"}
                  disabled={item.count !== (statistics.classification_statistics_saved ?? statistics.saved)}
                  onClick={() => setRevision({ runId: statistics.id, sha: item.taxonomy_sha256 })}>
                  {item.taxonomy_version} · {item.count} 篇
                </Button>)}
              </div>}
            </>}
            {statistics?.entries?.map((entry) => (
              <article
                key={entry.pmid}
                className="flex flex-col gap-2 border-b pb-3 last:border-0"
              >
                <a
                  className="font-medium underline"
                  href={
                    entry.material_id
                      ? `/evidence/sources/papers/?material=${entry.material_id}`
                      : `https://pubmed.ncbi.nlm.nih.gov/${entry.pmid}/`
                  }
                >
                  {entry.title || `PMID ${entry.pmid}`}
                </a>
                <div className="flex flex-wrap gap-2">
                  <Badge variant="outline">PMID {entry.pmid}</Badge>
                  <Badge variant="secondary">
                    {FULLTEXTS[entry.fulltext_state] ?? entry.fulltext_state}
                  </Badge>
                </div>
                <div className="flex flex-wrap gap-1">
                  {entry.classification?.labels.map((label) => (
                    <Badge
                      variant="outline"
                      key={`${label.axis}:${label.id}`}
                      title={label.matches
                        .map((match) => `${match.field}: ${match.text}`)
                        .join("；")}
                    >
                      {label.label}
                    </Badge>
                  ))}
                </div>
                {entry.message && (
                  <p className="text-xs text-muted-foreground">
                    {entry.message}
                  </p>
                )}
                {(entry.classification?.publication_status_conflicts?.length ??
                  0) > 0 && (
                  <p className="text-xs text-muted-foreground">
                    标题与来源出版类型存在冲突，按出版类型显示，冲突记录已保留。
                  </p>
                )}
              </article>
            ))}
            {statistics?.entries_truncated && (
              <p>此处展示前 100 篇，完整记录保存在批次数据库中。</p>
            )}
          </div>}
        </details>
      )}
      {run && (
        <details className="rounded-lg border bg-background p-4">
          <summary className="cursor-pointer font-medium">
            新建批次 / 历史回填
          </summary>
          <div className="mt-4">
            <NewCollection
              onCreated={(id) => {
                setSelected(id);
                refresh();
              }}
            />
          </div>
        </details>
      )}
      {(list.data?.results.length ?? 0) > 1 && (
        <div className="flex flex-wrap gap-2" aria-label="切换采集批次">
          {list.data!.results.map((item) => (
            <Button
              key={item.id}
              variant="outline"
              size="sm"
              onClick={() => setSelected(item.id)}
            >
              {item.start_date} — {item.end_date} · {STATES[item.state]}
            </Button>
          ))}
        </div>
      )}
    </div>
  );
}
