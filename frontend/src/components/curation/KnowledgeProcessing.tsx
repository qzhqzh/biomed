import { useCuration, date } from "@/lib/curation";
import { Button } from "@/components/ui/button";
import { ErrorMessage } from "@/components/curation/Common";

type Article = { pmid: string; queue_state: string; category?: string; effective_category?: string };
type Batch = {
  id: string; state: string; attempts: number; seconds: number; error: string; updated: number;
  summary: { articles?: Article[]; knowledge_records?: number; structurally_eligible?: number;
    offline_ready_count?: number; quality_status?: string; issues?: Record<string, number>;
    evaluation?: { reviewer: string; counts: Record<string, number>; lessons: string[] };
    target_readiness?: { structurally_eligible: number; counts: Record<string, number> } };
};
type Processing = {
  online: boolean; activity: string; reason: string; updated_at?: number;
  papers?: Record<string, number>; totals?: Record<string, number>;
  today?: { articles: number; seconds: number };
  limits?: { batch_size: number; daily_articles: number | null; daily_runtime_seconds: number | null };
  progress?: { batch: string; stage: string; completed?: number; total?: number };
  batches?: Batch[];
};
const STATES: Record<string, string> = {
  running: "处理中", batch_completed: "本批已完成", waiting: "等待下一批", waiting_budget: "等待下一日预算",
  waiting_gpu: "等待空闲显卡", waiting_storage: "等待存储恢复", waiting_environment: "等待运行环境恢复",
  waiting_resources: "等待主机资源空闲",
  waiting_process: "等待上次进程退出", paused: "已暂停", stopped: "已停止", unavailable: "未接通",
  completed: "已完成", partial: "部分完成", excluded: "分类排除", failed: "失败已隔离", retry: "等待重试", ready: "待执行",
  preparing: "准备来源", classification: "4B文献分类", screening: "9B段落筛选", entities: "9B实体抽取",
  relations: "Bonsai关系抽取", infer_entities: "9B实体抽取", infer_relations: "Bonsai关系抽取",
  entity_normalization: "实体归一", reporting: "入库与批次总结", assemble_knowledge: "知识组装", deliver: "生成交付包",
};
const hours = (seconds = 0) => (seconds / 3600).toFixed(2);

export function KnowledgeProcessing() {
  const status = useCuration<Processing>("knowledge-processing/", 15000);
  const data = status.data;
  const papers = data?.papers ?? {};
  const processed = (papers.completed ?? 0) + (papers.partial ?? 0) + (papers.excluded ?? 0) + (papers.failed ?? 0);
  return (
    <section aria-labelledby="knowledge-processing-title" className="space-y-4 rounded-lg border p-4 sm:p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="knowledge-processing-title" className="font-semibold">自动知识处理</h2>
          <p className="mt-1 text-sm text-muted-foreground">自动领取已有全文 → 分类与提取 → 实体归一 → 知识入库与兼容预览</p>
        </div>
        <Button variant="outline" size="sm" onClick={status.refresh}>刷新处理状态</Button>
      </div>
      <ErrorMessage error={status.error} />
      <div role="status" className="flex flex-wrap gap-x-5 gap-y-2 text-sm">
        <span>{data?.online ? "服务在线" : "未检测到在线服务"} · {STATES[data?.activity ?? ""] ?? "读取中"}</span>
        {data?.updated_at && <span className="text-muted-foreground">更新于 {date(new Date(data.updated_at * 1000).toISOString())}</span>}
      </div>
      {data?.reason && <p className="text-sm text-muted-foreground">{data.reason}</p>}
      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        {[ ["已处理文章", processed], ["待处理文章", (papers.queued ?? 0) + (papers.assigned ?? 0)],
          ["保存记录", data?.totals?.knowledge_records ?? 0], ["结构合格", data?.totals?.structurally_eligible ?? 0] ].map(([label, value]) => (
          <div key={label}><dt className="text-xs text-muted-foreground">{label}</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{value}</dd></div>
        ))}
      </dl>
      <p className="text-xs/relaxed text-muted-foreground">
        历史已有知识 {papers.legacy ?? 0} 篇另计；部分完成 {papers.partial ?? 0} 篇，分类排除 {papers.excluded ?? 0} 篇，失败隔离 {papers.failed ?? 0} 篇。
        结构合格与语义正确率分开；本服务保存后仍需独立质量抽查，兼容预览不写入tumor_api。
      </p>
      {data?.limits && <p className="text-sm">
        每批 {data.limits.batch_size} 篇 · 今日领取 {data.today?.articles ?? 0} 篇
        {data.limits.daily_articles == null ? "（不限篇数）" : `（上限 ${data.limits.daily_articles} 篇）`}
        · 今日运行 {hours(data.today?.seconds)} 小时
        {data.limits.daily_runtime_seconds == null ? "（不限时长）" : `（上限 ${hours(data.limits.daily_runtime_seconds)} 小时）`}
        · 单GPU串行
        {data.limits.daily_articles == null && data.limits.daily_runtime_seconds == null && " · 资源空闲时持续处理"}
      </p>}
      {data?.progress && <p className="rounded bg-muted/50 p-3 text-sm">
        当前：{STATES[data.progress.stage] ?? data.progress.stage}
        {typeof data.progress.completed === "number" && <> · 已处理 {data.progress.completed}{data.progress.total ? `/${data.progress.total}` : ""} 项</>}
        <span className="mt-1 block break-all text-xs text-muted-foreground">{data.progress.batch}</span>
      </p>}
      {data?.batches?.length ? <div className="divide-y border-t">
        {data.batches.map(batch => <details key={batch.id} className="py-3">
          <summary className="cursor-pointer break-words text-sm">
            {STATES[batch.state] ?? batch.state} · {batch.summary.articles?.length ?? data.limits?.batch_size ?? "—"} 篇
            · {batch.summary.knowledge_records ?? 0} 条记录 · {hours(batch.seconds)} 小时
            <span className="ml-2 text-xs text-muted-foreground">{batch.id}</span>
          </summary>
          <div className="mt-3 space-y-2 pl-3 text-sm">
            {batch.error && <p className="break-words text-destructive">{batch.error}</p>}
            <p>执行尝试 {batch.attempts} 次 · 结构合格 {batch.summary.structurally_eligible ?? 0} 条 · 目标离线结构通过 {batch.summary.offline_ready_count ?? 0} 条</p>
            {batch.summary.target_readiness && <p className="rounded bg-muted/50 p-3 text-sm/relaxed">
              结构合格的 {batch.summary.target_readiness.structurally_eligible} 条中：
              当前证据中心不能表达 {batch.summary.target_readiness.counts.outside_target ?? 0} 条，
              条件或映射仍需处理 {batch.summary.target_readiness.counts.mapping_needed ?? 0} 条，
              等待自动补实体 {batch.summary.target_readiness.counts.entity_sync ?? 0} 条，
              离线结构通过 {batch.summary.target_readiness.counts.offline_ready ?? 0} 条。
              目标范围不适用的知识仍保留在 biomed；不能把它们都当作补 ID 的任务或提取错误。
            </p>}
            {batch.summary.evaluation ? <div className="space-y-1 rounded bg-muted/50 p-3 text-sm">
              <p>固定样本核读：支持 {batch.summary.evaluation.counts.supported ?? 0} 条、不支持 {batch.summary.evaluation.counts.unsupported ?? 0} 条、未确定 {batch.summary.evaluation.counts.uncertain ?? 0} 条。</p>
              <p className="text-xs text-muted-foreground">{batch.summary.evaluation.reviewer} · 不代表全批准确率，核验不阻断知识保存。</p>
              {batch.summary.evaluation.lessons.map((lesson, index) => <p key={index}>{lesson}</p>)}
            </div> : <p className="text-muted-foreground">语义质量待独立核验；每批保留固定抽查样本。</p>}
            {!!batch.summary.knowledge_records && <a className="text-primary underline" href={`/knowledge/candidates/?source_run=${encodeURIComponent(batch.id)}`}>查看本批知识与原文出处</a>}
            {batch.summary.articles?.map(article => <p key={article.pmid}>PMID {article.pmid} · {STATES[article.queue_state] ?? article.queue_state}</p>)}
            {Object.entries(batch.summary.issues ?? {}).map(([code, count]) => <p key={code} className="break-all text-xs text-muted-foreground">{code}：{count}</p>)}
          </div>
        </details>)}
      </div> : <p className="text-sm text-muted-foreground">暂无批次记录。</p>}
    </section>
  );
}
