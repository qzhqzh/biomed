import { useState } from "react";
import { AppShell } from "@/layouts/AppShell";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { ErrorMessage, NoData, Status } from "@/components/curation/Common";
import {
  curation,
  date,
  useCuration,
  KINDS,
  type Overview,
  type Work,
} from "@/lib/curation";

export default function WorkflowsApp() {
  const [selected, setSelected] = useState<string | null>(() =>
    new URLSearchParams(location.search).get("job"),
  );
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const list = useCuration<{ total: number; results: Work[] }>(
    `workflows/?offset=${offset}`,
    5000,
  );
  const overview = useCuration<Overview>("overview/", 5000);
  const detail = useCuration<Work>(
    selected ? `workflows/${selected}/` : null,
    5000,
  );
  async function retry() {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      await curation(`workflows/${selected}/`, {
        method: "POST",
        body: JSON.stringify({ action: "retry" }),
      });
      detail.refresh();
      list.refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <AppShell
      title="工作流"
      description="获取资料 → 解析原文 → 抽取候选证据 → AI 审核发布 → 人工抽查。每个任务都有执行记录。"
      crumbs={[{ label: "管理" }, { label: "工作流" }]}
      actions={
        <Button asChild variant="outline" className="h-9">
          <a href="/evidence/sources/">添加资料</a>
        </Button>
      }
    >
      <div className="flex flex-wrap gap-x-8 gap-y-3 border-y py-4 text-sm">
        <span>
          执行服务：
          {overview.data
            ? overview.data.worker_online
              ? "在线"
              : "未检测到心跳"
            : "读取中…"}
        </span>
        <span>
          知识模型：
          {overview.data?.model_configured ? "已配置适配器" : "尚未配置"}
        </span>
        <span>发布规则：AI 通过后发布 · 人工抽查</span>
      </div>
      {!overview.data?.model_configured && (
        <p className="text-sm/relaxed text-muted-foreground">
          文献下载和基础文本解析可以执行。指南当前先归档，等待定义知识结构。尚未配置的知识抽取会保留在“待配置模型”。
        </p>
      )}
      <ErrorMessage error={error || list.error || overview.error} />
      {list.loading && <p role="status">正在读取任务…</p>}
      {list.data?.results.length ? (
        <div className="divide-y border-y">
          {list.data.results.map((job) => (
            <button
              key={job.id}
              type="button"
              onClick={() => setSelected(job.id)}
              className="flex w-full flex-wrap items-start gap-4 py-4 text-left hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-ring"
            >
              <div className="min-w-0 flex-1">
                <h2 className="text-sm font-semibold">
                  {KINDS[job.kind] ?? job.kind}
                  {job.payload.namespace ? ` · ${job.payload.namespace}` : ""}
                </h2>
                <p className="mt-1 text-xs text-muted-foreground">
                  {date(job.created_at)} · 已执行 {job.attempts} 次
                </p>
                {job.message && (
                  <p className="mt-2 text-sm/relaxed">{job.message}</p>
                )}
              </div>
              <Status value={job.state} />
            </button>
          ))}
        </div>
      ) : (
        !list.loading && (
          <NoData
            title="还没有任务"
            description="资料下载、选择启动的解析和标准库更新都在这里记录。仅归档文件不会创建解析任务。"
          />
        )
      )}
      {list.data && list.data.total > 50 && (
        <div className="flex justify-between">
          <Button
            variant="outline"
            disabled={!offset}
            onClick={() => setOffset(offset - 50)}
          >
            上一页
          </Button>
          <Button
            variant="outline"
            disabled={offset + 50 >= list.data.total}
            onClick={() => setOffset(offset + 50)}
          >
            下一页
          </Button>
        </div>
      )}
      <Sheet
        open={Boolean(selected)}
        onOpenChange={(open) => {
          if (!open) setSelected(null);
        }}
      >
        <SheetContent className="data-[side=right]:w-full overflow-y-auto data-[side=right]:sm:max-w-xl">
          <SheetHeader>
            <SheetTitle>
              {KINDS[detail.data?.kind ?? ""] ?? "任务详情"}
            </SheetTitle>
            <SheetDescription>
              查看执行状态、重试记录和后续步骤。
            </SheetDescription>
          </SheetHeader>
          <div className="flex flex-col gap-5 p-5 text-sm">
            <ErrorMessage error={detail.error || error} />
            {detail.data && (
              <>
                <Status value={detail.data.state} />
                <p className="leading-relaxed">
                  {detail.data.message || "任务记录已保存。"}
                </p>
                {["failed", "blocked", "waiting_configuration"].includes(
                  detail.data.state,
                ) && (
                  <Button
                    disabled={
                      busy ||
                      (detail.data.state === "waiting_configuration" &&
                        !overview.data?.model_configured)
                    }
                    onClick={retry}
                    className="h-10"
                  >
                    {detail.data.state === "waiting_configuration" &&
                    !overview.data?.model_configured
                      ? "配置模型后可重试"
                      : "重试此任务"}
                  </Button>
                )}
                <dl className="space-y-2 break-all">
                  <div>
                    <dt className="text-muted-foreground">任务 ID</dt>
                    <dd>{detail.data.id}</dd>
                  </div>
                  {Object.entries(detail.data.payload)
                    .filter(([key]) => key !== "provenance")
                    .map(([key, value]) => (
                      <div key={key}>
                        <dt className="text-muted-foreground">{key}</dt>
                        <dd>{String(value)}</dd>
                      </div>
                    ))}
                </dl>
                {typeof detail.data.result.material_id === "string" && (
                  <a
                    className="text-primary underline"
                    href={`/evidence/sources/?material=${detail.data.result.material_id}`}
                  >
                    查看入库资料
                  </a>
                )}
                {typeof detail.data.result.count === "number" && (
                  <p>处理记录数：{detail.data.result.count.toLocaleString()}</p>
                )}
                <h3 className="border-t pt-4 font-semibold">执行记录</h3>
                {detail.data.runs?.map((run) => (
                  <div key={run.number} className="space-y-2 border-b pb-4">
                    <div className="flex items-center justify-between">
                      <span>
                        第 {run.number} 次 · {date(run.started_at)}
                      </span>
                      <Status value={run.outcome} />
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {run.finished_at
                        ? `结束：${date(run.finished_at)}`
                        : "尚无结束时间"}
                    </p>
                    {run.message && <p>{run.message}</p>}
                  </div>
                ))}
              </>
            )}
          </div>
        </SheetContent>
      </Sheet>
    </AppShell>
  );
}
