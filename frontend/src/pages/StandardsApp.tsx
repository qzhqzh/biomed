import { useState, type FormEvent } from "react";
import { Search, ExternalLink, ArrowRight } from "lucide-react";
import { AppShell } from "@/layouts/AppShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Field, FieldLabel } from "@/components/ui/field";
import { Badge } from "@/components/ui/badge";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { ErrorMessage, NoData } from "@/components/curation/Common";
import {
  curation,
  useCuration,
  type Concept,
  type Release,
  type Work,
} from "@/lib/curation";

export default function StandardsApp() {
  const params = new URLSearchParams(location.search);
  const namespace =
    location.pathname === "/knowledge/drugs/" ||
    (location.pathname === "/knowledge/standards/" &&
      params.get("namespace") === "RXNORM")
      ? "RXNORM"
      : "MONDO";
  const [input, setInput] = useState(params.get("q") ?? "");
  const [query, setQuery] = useState(input);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Concept | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const {
    data,
    error: loadError,
    loading,
  } = useCuration<{
    release: Release | null;
    total: number;
    results: Concept[];
  }>(
    `concepts/?namespace=${namespace}&q=${encodeURIComponent(query)}&offset=${offset}`,
  );
  function search(event: FormEvent) {
    event.preventDefault();
    setQuery(input.trim());
    setOffset(0);
    setSelected(null);
  }
  async function update() {
    setBusy(true);
    setError("");
    try {
      const job = await curation<Work>("sources/", {
        method: "POST",
        body: JSON.stringify({ namespace }),
      });
      setNotice(
        `任务已${job.state === "succeeded" ? "完成，可刷新查看" : "提交，可在工作流中查看"}。`,
      );
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <AppShell
      title={namespace === "MONDO" ? "疾病" : "药物"}
      description={
        namespace === "MONDO"
          ? "以疾病为入口组织标准信息、相关药物与临床证据。"
          : "独立维护药物标准名称和标识，通过有来源的证据与疾病知识关联。"
      }
      crumbs={[
        { label: "知识库" },
        { label: namespace === "MONDO" ? "疾病" : "药物" },
      ]}
      actions={
        <Button asChild variant="outline" className="h-9">
          <a href="/management/workflows/">更新任务</a>
        </Button>
      }
    >
      <form onSubmit={search} className="flex items-end gap-3">
        <Field className="flex-1">
          <FieldLabel htmlFor="standard-query">
            名称、同义词或标准 ID
          </FieldLabel>
          <Input
            id="standard-query"
            className="h-10 text-sm"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={
              namespace === "MONDO"
                ? "例如 lung cancer、MONDO:0008903"
                : "例如 osimertinib、RXNORM:1721560"
            }
          />
        </Field>
        <Button className="h-10 px-5 text-sm">
          <Search />
          查询
        </Button>
      </form>
      <ErrorMessage error={error || loadError} />
      <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
        <p className="text-muted-foreground">
          {data?.release
            ? `${data.release.version} · ${data.release.count.toLocaleString()} 个实体 · 当前检索 ${data.total.toLocaleString()} 条`
            : loading
              ? "正在读取版本…"
              : "尚未导入这个标准库"}
        </p>
        <Button
          type="button"
          variant="outline"
          className="h-9"
          disabled={busy}
          onClick={update}
        >
          {busy ? "提交中…" : "从官方来源更新"}
        </Button>
      </div>
      {notice && (
        <p role="status" className="text-sm">
          {notice}{" "}
          <a className="text-primary underline" href="/management/workflows/">
            查看工作流
          </a>
        </p>
      )}
      <p className="text-sm/relaxed text-muted-foreground">
        {namespace === "MONDO"
          ? "保留疾病同义词、父级与外部交叉引用。交叉引用仅供核对，不自动声明两个数据库的条目等价。"
          : "当前为 RxNorm 的成分实体（IN），不表示中国上市状态、商品名覆盖或获批适应症。"}
      </p>
      {data?.results.length ? (
        <div className="divide-y border-y">
          {data.results.map((row) => (
            <button
              key={row.id}
              type="button"
              className="flex w-full items-start gap-4 py-4 text-left hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-ring"
              onClick={() => setSelected(row)}
            >
              <div className="min-w-0 flex-1">
                <h3 className="break-words text-sm font-semibold">
                  {row.label}
                </h3>
                <p className="mt-1 text-xs text-muted-foreground">
                  {row.identifier}
                </p>
              </div>
              <ArrowRight className="mt-1 size-4 shrink-0" />
            </button>
          ))}
        </div>
      ) : (
        !loading && (
          <NoData
            title={data?.release ? "没有找到对应实体" : "标准库待导入"}
            description={
              data?.release
                ? "尝试来源中的英文名称或准确 ID。缺失条目不会自动补全。"
                : "点击“从官方来源更新”，任务完成后重新打开本页。"
            }
          />
        )
      )}
      {data && data.total > 50 && (
        <div className="flex items-center justify-between">
          <Button
            variant="outline"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - 50))}
          >
            上一页
          </Button>
          <span className="text-sm">
            {offset + 1}–{Math.min(offset + 50, data.total)} / {data.total}
          </span>
          <Button
            variant="outline"
            disabled={offset + 50 >= data.total}
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
            <SheetTitle>{selected?.label}</SheetTitle>
            <SheetDescription>标准实体的标识、出处与版本</SheetDescription>
          </SheetHeader>
          {selected && (
            <div className="flex flex-col gap-6 p-5 text-sm">
              <Badge variant="outline" className="w-fit">
                {selected.identifier}
              </Badge>
              {selected.definition && (
                <p className="leading-relaxed">{selected.definition}</p>
              )}
              <div>
                <h3 className="mb-2 font-semibold">同义词</h3>
                <p className="break-words leading-relaxed">
                  {selected.synonyms.join(" · ") || "来源未提供"}
                </p>
              </div>
              <div>
                <h3 className="mb-2 font-semibold">父级标识</h3>
                <p>{selected.parents.join(" · ") || "来源未提供"}</p>
              </div>
              <div>
                <h3 className="mb-2 font-semibold">交叉引用（需核对关系）</h3>
                <p className="break-words leading-relaxed">
                  {selected.cross_references.join(" · ") || "来源未提供"}
                </p>
              </div>
              {namespace === "MONDO" && (
                <section className="border-t pt-4" aria-label="疾病相关药物">
                  <h3 className="font-semibold">相关药物</h3>
                  <p className="mt-2 text-sm/relaxed text-muted-foreground">
                    疾病与药物的知识关联尚未建立。后续按已发布证据整理适用条件和原文出处；药物标准名称与标识可在独立药物库查询。
                  </p>
                  <Button asChild variant="outline" className="mt-3 h-10">
                    <a href="/knowledge/drugs/">查看药物标准库</a>
                  </Button>
                </section>
              )}
              <div className="space-y-2 border-t pt-4">
                <p>来源版本：{data?.release?.version}</p>
                <p className="break-words">{data?.release?.rights}</p>
                <a
                  href={data?.release?.source_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 text-primary underline"
                >
                  官方来源
                  <ExternalLink className="size-3" />
                </a>
              </div>
              <Button asChild variant="outline" className="h-10">
                <a
                  href={`/evidence/clinical/?q=${encodeURIComponent(selected.identifier)}`}
                >
                  查看关联证据
                </a>
              </Button>
            </div>
          )}
        </SheetContent>
      </Sheet>
    </AppShell>
  );
}
