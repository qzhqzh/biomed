import { useState } from "react";
import { ArrowRight, FileCheck2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ErrorMessage, Status } from "@/components/curation/Common";
import {
  date,
  KINDS,
  useCuration,
  type Version,
  type Work,
} from "@/lib/curation";

type Processing = {
  version_id: string;
  archived_at: string;
  stages: { parse: Work | null; extract: Work | null };
  total: number;
  results: Work[];
};

// This is a proposed method, not a saved schema or an executable pipeline.
const GUIDE_STEPS = [
  [
    "定义知识结构",
    "先确定要保存的知识、实体关系、适用条件和原文定位要求。结构与字段由你下一步确定。",
  ],
  [
    "选取代表页试验",
    "结构确定并确认处理范围后，选少量正文、表格和诊疗路径页，建立人工核对样本。",
  ],
  [
    "按章节解析原文",
    "分别处理正文、表格、路径图和脚注；同时保留 PDF 物理页码、指南页码及跨页引用。",
  ],
  [
    "按结构抽取与核对",
    "将原文映射到约定结构，核对标准实体 ID。缺失条件保持未知，原文与处理版本始终可追溯。",
  ],
  [
    "审核与发布",
    "知识抽取和 AI 审核分别记录。AI 审核通过后发布，人工抽查可确认或撤回，保留历史。",
  ],
];

export function MaterialProcessing({
  version,
  guideline,
  mode,
}: {
  version: Version;
  guideline: boolean;
  mode: "plan" | "history";
}) {
  const [offset, setOffset] = useState(0);
  const { data, error, loading } = useCuration<Processing>(
    `versions/${version.id}/processing/?offset=${offset}&limit=10`,
    10000,
  );
  return (
    <div className="flex flex-col gap-5 pt-4">
      <ErrorMessage error={error} />
      {mode === "plan" ? (
        <>
          {guideline && (
            <section
              className="rounded-lg bg-muted/50 p-4"
              aria-label="指南解析方案"
            >
              <Badge variant="outline">待定义知识结构</Badge>
              <h3 className="mt-3 font-semibold">
                先确定要形成什么知识，再选择解析方式
              </h3>
              <p className="mt-2 text-sm/relaxed text-muted-foreground">
                下面是待确认的解析方案，知识结构尚未配置。本阶段先归档原文；已执行的任务以实际状态和处理记录为准。
              </p>
            </section>
          )}
          <section aria-label="当前版本处理状态">
            <h3 className="mb-3 font-semibold">当前版本处理状态</h3>
            {loading && <p role="status">正在读取任务状态…</p>}
            {data && (
              <dl className="divide-y border-y">
                {(["parse", "extract"] as const).map((stage) => {
                  const job = data.stages[stage];
                  return (
                    <div
                      key={stage}
                      className="flex flex-wrap items-start justify-between gap-3 py-3"
                    >
                      <dt>{KINDS[stage]}</dt>
                      <dd className="flex max-w-full flex-col items-end gap-2">
                        {job ? (
                          <Status value={job.state} />
                        ) : (
                          <Badge variant="outline">未启动</Badge>
                        )}
                        {job && (
                          <a
                            className="text-primary underline"
                            href={`/management/workflows/?job=${job.id}`}
                          >
                            查看任务及执行记录
                          </a>
                        )}
                      </dd>
                    </div>
                  );
                })}
              </dl>
            )}
          </section>
          {guideline && (
            <section aria-label="拟定解析步骤">
              <h3 className="mb-1 font-semibold">拟定解析步骤</h3>
              <ol className="divide-y">
                {GUIDE_STEPS.map(([title, description], index) => (
                  <li key={title} className="flex gap-3 py-4">
                    <span className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-md bg-muted text-xs">
                      {index + 1}
                    </span>
                    <div>
                      <h4 className="font-medium">{title}</h4>
                      <p className="mt-1 text-sm/relaxed text-muted-foreground">
                        {description}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
              <p className="border-t pt-3 text-sm/relaxed text-muted-foreground">
                后续每次处理都应固定原文件版本、知识结构版本、解析范围与工具版本；重跑新增记录，保留失败原因和旧结果。
              </p>
            </section>
          )}
        </>
      ) : (
        <>
          <section
            className="flex gap-3 border-b pb-4"
            aria-label="原文件归档记录"
          >
            <FileCheck2 className="mt-1 size-5 shrink-0 text-primary" />
            <div className="min-w-0">
              <h3 className="font-semibold">原文件已归档</h3>
              <p className="mt-1 text-muted-foreground">
                {date(version.acquired_at)}
              </p>
              <p className="mt-2 break-words">{version.filename}</p>
              <p className="mt-2 break-all text-xs text-muted-foreground">
                SHA-256：{version.sha256}
              </p>
            </div>
          </section>
          <section aria-label="解析与抽取记录" className="flex flex-col gap-3">
            <h3 className="font-semibold">
              解析与抽取记录{data ? ` · ${data.total} 个任务` : ""}
            </h3>
            <p className="text-sm/relaxed text-muted-foreground">
              仅展示当前文件版本的真实任务；打开任务可查看每次执行、开始与结束时间、失败原因和重试记录。
            </p>
            {loading && <p role="status">正在读取处理记录…</p>}
            {data?.total === 0 && (
              <p className="rounded-lg bg-muted/50 p-4 leading-relaxed">
                尚未创建解析或抽取任务。原文件归档不代表解析完成。
              </p>
            )}
            {data?.results.map((job) => (
              <a
                key={job.id}
                className="flex items-start gap-3 border-b py-4 hover:bg-muted/40"
                href={`/management/workflows/?job=${job.id}`}
              >
                <div className="min-w-0 flex-1">
                  <h4 className="font-medium">{KINDS[job.kind]}</h4>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {date(job.created_at)} · 已执行 {job.attempts} 次
                  </p>
                  {job.payload.parser_version && (
                    <p className="mt-2 break-words text-xs text-muted-foreground">
                      解析版本：{job.payload.parser_version}
                    </p>
                  )}
                  {job.message && (
                    <p className="mt-2 break-words leading-relaxed">
                      {job.message}
                    </p>
                  )}
                </div>
                <Status value={job.state} />
                <ArrowRight className="mt-1 size-4 shrink-0" />
              </a>
            ))}
            {data && data.total > 10 && (
              <div className="flex items-center justify-between gap-3">
                <Button
                  variant="outline"
                  disabled={!offset}
                  onClick={() => setOffset(offset - 10)}
                >
                  上一页记录
                </Button>
                <span className="text-xs text-muted-foreground">
                  {offset + 1}–{Math.min(offset + 10, data.total)} /{" "}
                  {data.total}
                </span>
                <Button
                  variant="outline"
                  disabled={offset + 10 >= data.total}
                  onClick={() => setOffset(offset + 10)}
                >
                  下一页记录
                </Button>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
