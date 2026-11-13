import { useState, type FormEvent } from "react";
import { Search, ExternalLink, ArrowRight } from "lucide-react";
import { AppShell } from "@/layouts/AppShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Field, FieldLabel } from "@/components/ui/field";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { ErrorMessage, NoData } from "@/components/curation/Common";
import { DrugResourceDetails } from "@/components/curation/DrugResourceDetails";
import { CancerTreeBrowser } from "@/components/curation/CancerTreeBrowser";
import { DrugTreeBrowser } from "@/components/curation/DrugTreeBrowser";
import { TranslationLegend } from "@/components/curation/TranslationLegend";
import { preferredChinese, TRANSLATION_STATUS } from "@/lib/standard-labels";
import {
  curation,
  useCuration,
  type Concept,
  type Release,
  type Work,
} from "@/lib/curation";

type ClassificationNode = { identifier: string; label: string; translations?: Concept["translations"] };

function nodeLabel(node: ClassificationNode) {
  const translation = preferredChinese(node);
  return translation ? `${translation.label} / ${node.label}` : node.label;
}

export default function StandardsApp() {
  const params = new URLSearchParams(location.search);
  const namespace =
    location.pathname === "/knowledge/drugs/" ||
    (location.pathname === "/knowledge/standards/" &&
      params.get("namespace") === "RXNORM")
      ? "RXNORM"
      : location.pathname === "/knowledge/cancers/" || params.get("namespace") === "ONCOTREE"
        ? "ONCOTREE"
        : "MONDO";
  const title = { MONDO: "疾病", RXNORM: "药物", ONCOTREE: "癌种分类" }[namespace];
  const isCancer = namespace === "ONCOTREE";
  const isDrug = namespace === "RXNORM";
  const [drugView, setDrugView] = useState(params.get("q") || params.get("view") === "ingredients" ? "ingredients" : "tree");
  const drugTree = isDrug && drugView === "tree";
  const hierarchical = namespace === "MONDO";
  const bilingual = namespace === "RXNORM" || namespace === "ONCOTREE";
  const [input, setInput] = useState(params.get("q") ?? "");
  const [query, setQuery] = useState(input);
  const [parent, setParent] = useState<string | null>(params.get("parent") ??
    (namespace === "ONCOTREE" && !params.get("q") ? "ONCOTREE:TISSUE" : null));
  const [releaseId, setReleaseId] = useState(params.get("release") ?? "");
  const [revealIdentifier, setRevealIdentifier] = useState<string | null>(params.get("parent"));
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Concept | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const selectedChinese = bilingual && selected ? preferredChinese(selected) : undefined;
  const {
    data,
    error: loadError,
    loading,
  } = useCuration<{
    release: Release | null;
    total: number;
    results: Concept[];
    navigation: {
      focus: ClassificationNode | null;
      parents: ClassificationNode[];
      missing_parents: string[];
    } | null;
  }>(
    (isCancer
      ? "concepts/?namespace=ONCOTREE&parent=ONCOTREE:TISSUE&offset=0"
      : drugTree ? "concepts/?namespace=RXNORM&limit=1"
      : `concepts/?namespace=${namespace}&q=${encodeURIComponent(query)}&offset=${offset}` +
        (parent !== null ? `&parent=${encodeURIComponent(parent)}` : "")) +
      (releaseId ? `&release=${encodeURIComponent(releaseId)}` : ""),
  );
  function browse(identifier: string | null) {
    if (isCancer) {
      setRevealIdentifier(identifier);
      setSelected(null);
      return;
    }
    setReleaseId(data?.release?.id ?? releaseId);
    setParent(identifier);
    setOffset(0);
    setInput("");
    setQuery("");
    setSelected(null);
  }
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
      title={title}
      description={
        namespace === "MONDO"
          ? "以疾病为入口组织标准信息、相关药物与临床证据。"
          : namespace === "ONCOTREE"
            ? "按组织、癌种和亚型浏览 OncoTree，保留官方代码、层级和来源版本。"
            : "独立维护药物标准名称和标识，通过有来源的证据与疾病知识关联。"
      }
      crumbs={[
        { label: "知识库" },
        { label: title },
      ]}
      actions={
        <Button asChild variant="outline" className="h-9">
          <a href="/management/workflows/">更新任务</a>
        </Button>
      }
    >
      {hierarchical && (
        <section aria-label="分类导航" className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="outline" onClick={() => browse(null)}>
              全库检索
            </Button>
            <Button type="button" variant="outline" onClick={() => browse("__roots__")}>
              分类起点
            </Button>
            {data?.navigation?.parents.map((node) => (
              <Button key={node.identifier} type="button" variant="outline"
                onClick={() => browse(node.identifier)}>
                上级：{nodeLabel(node)}
              </Button>
            ))}
          </div>
          <p className="text-sm text-muted-foreground">
            {parent === null ? "搜索整个标准库；也可从分类起点逐层浏览。" :
              data?.navigation?.focus
                ? `当前分类：${nodeLabel(data.navigation.focus)} · ${data.navigation.focus.identifier}（下方为直接子类）`
                : "本库分类入口（部分上级位于外部本体）"}
          </p>
          {Boolean(data?.navigation?.missing_parents.length) && (
            <p className="text-sm text-muted-foreground">
              本版本未收录的上级：{data?.navigation?.missing_parents.join(" · ")}
            </p>
          )}
        </section>
      )}
      {isDrug && <Tabs value={drugView} onValueChange={(value) => {
        setReleaseId(data?.release?.id ?? releaseId);
        setDrugView(value);
        setSelected(null);
      }}>
        <TabsList aria-label="药物浏览方式">
          <TabsTrigger id="drug-tree-tab" aria-controls="drug-browse-panel" value="tree">药理分类树</TabsTrigger>
          <TabsTrigger id="drug-ingredients-tab" aria-controls="drug-browse-panel" value="ingredients">全部药物成分</TabsTrigger>
        </TabsList>
      </Tabs>}
      {!isCancer && !drugTree && <form onSubmit={search} className="flex items-end gap-3">
        <Field className="flex-1">
          <FieldLabel htmlFor="standard-query">
            {parent === null
              ? bilingual ? "中文、英文、同义词或标准 ID" : "名称、同义词或标准 ID"
              : bilingual ? "在当前层级中搜索中文、英文或 ID" : "在当前层级中搜索"}
          </FieldLabel>
          <Input
            id="standard-query"
            className="h-10 text-sm"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={
              namespace === "MONDO"
                ? "例如 lung cancer、MONDO:0008903"
                : "例如 奥希替尼、osimertinib、RXNORM:1721560"
            }
          />
        </Field>
        <Button className="h-10 px-5 text-sm">
          <Search />
          查询
        </Button>
      </form>}
      <ErrorMessage error={error || loadError} />
      <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
        <p className="text-muted-foreground">
          {data?.release
            ? isCancer
              ? `${data.release.version} · 全库 ${data.release.count.toLocaleString()} 个分类节点 · ${data.total.toLocaleString()} 个一级分组`
              : drugTree ? `${data.release.version} · 全库 ${data.release.count.toLocaleString()} 个药物成分`
              : `${data.release.version} · ${data.release.count.toLocaleString()} 个实体 · 当前检索 ${data.total.toLocaleString()} 条`
            : loading
              ? "正在读取版本…"
              : loadError ? "来源版本读取失败" : "尚未导入这个标准库"}
        </p>
        <Button
          type="button"
          variant="outline"
          className="h-9"
          disabled={busy}
          onClick={update}
        >
          {busy ? "提交中…" : namespace === "ONCOTREE" ? "重新同步固定版本" : "从官方来源更新"}
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
          ? "疾病可以有多个父级；按来源保留全部分类路径。交叉引用仅供核对，不自动声明两个数据库的条目等价。"
          : namespace === "ONCOTREE"
            ? "包含实体瘤、血液肿瘤及组织分组节点；节点总数不等于癌种数量。分类不表示分期、适用治疗或入组资格，跨库交叉引用仍需核对。"
          : "当前为 RxNorm 的成分实体（IN），不表示中国上市状态、商品名覆盖或获批适应症。"}
      </p>
      {isDrug && !drugTree && <TranslationLegend />}
      <div id={isDrug ? "drug-browse-panel" : undefined} role={isDrug ? "tabpanel" : undefined}
        aria-labelledby={isDrug ? `drug-${drugView === "tree" ? "tree" : "ingredients"}-tab` : undefined}>
      {isCancer ? (
        data?.release ? (
          <CancerTreeBrowser
            key={data.release.id}
            release={data.release}
            roots={data.results}
            rootTotal={data.total}
            initialQuery={params.get("q") ?? ""}
            revealIdentifier={revealIdentifier}
            onSelect={(node) => {
              setRevealIdentifier(null);
              setSelected(node);
            }}
          />
        ) : loading ? (
          <p role="status" className="text-sm text-muted-foreground">正在读取癌种分类树…</p>
        ) : !loadError ? (
          <NoData title="癌种分类待导入" description="点击“重新同步固定版本”，任务完成后重新打开本页。" />
        ) : null
      ) : drugTree ? (
        data?.release ? <DrugTreeBrowser key={data.release.id} release={data.release} onSelect={setSelected} /> :
          loading ? <p role="status" className="text-sm text-muted-foreground">正在读取药物分类…</p> :
          !loadError ? <NoData title="药物成分库待导入" description="先导入 RxNorm 成分版本，再查看药理分类。" /> : null
      ) : data?.results.length ? (
        <div className="divide-y border-y">
          {data.results.map((row) => {
            const chinese = bilingual ? preferredChinese(row) : undefined;
            const RowLabel = isDrug ? "div" : "button";
            return (
            <div key={row.id} className="flex items-center gap-3 border-b last:border-b-0">
            <RowLabel
              {...(!isDrug ? { type: "button" as const, onClick: () => setSelected(row) } : {})}
              className={`flex min-w-0 flex-1 items-start gap-4 py-4 text-left ${!isDrug ? "hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-ring" : ""}`}
            >
              <div className="min-w-0 flex-1">
                <h3 className="flex flex-wrap items-center gap-2 break-words text-sm font-semibold">
                  {chinese?.label ?? row.label}
                  {chinese && <Badge variant="outline">{TRANSLATION_STATUS[chinese.status]}</Badge>}
                </h3>
                {bilingual && <p className="mt-1 break-words text-sm text-muted-foreground">
                  {chinese ? row.label : "中文待补充"}
                </p>}
                <p className="mt-1 text-xs text-muted-foreground">
                  {row.identifier}
                </p>
              </div>
              {!isDrug && <ArrowRight className="mt-1 size-4 shrink-0" />}
            </RowLabel>
            {isDrug && <Button type="button" variant="outline" aria-label={`查看${chinese?.label ?? row.label}详情`} onClick={() => setSelected(row)}>详情</Button>}
            {hierarchical && (
              <Button type="button" variant="outline" onClick={() => browse(row.identifier)}>
                下级分类
              </Button>
            )}
            </div>
            );
          })}
        </div>
      ) : (
        !loading && (
          <NoData
            title={data?.release ? parent !== null && !query ? "没有下级分类" : "没有找到对应实体" : "标准库待导入"}
            description={
              data?.release
                ? parent !== null && !query ? "当前分类在这个版本中没有直接子类，可返回上级或全库检索。" : bilingual ? "尝试已收录的中文名、来源英文名称或准确 ID。缺失条目不会自动补全。" : "尝试来源中的英文名称或准确 ID。缺失条目不会自动补全。"
                : "点击“从官方来源更新”，任务完成后重新打开本页。"
            }
          />
        )
      )}
      </div>
      {!isCancer && !drugTree && data && data.total > 50 && (
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
            <SheetTitle className="break-words">{selectedChinese?.label ?? selected?.label}</SheetTitle>
            <SheetDescription className="break-words">
              {bilingual && <>{selectedChinese ? selected?.label : "中文待补充"} · </>}标准实体的标识、出处与版本
            </SheetDescription>
          </SheetHeader>
          {selected && (
            <div className="flex flex-col gap-6 p-5 text-sm">
              <Badge variant="outline" className="w-fit">
                {selected.identifier}
              </Badge>
              {selectedChinese && <Badge variant="outline">{TRANSLATION_STATUS[selectedChinese.status]}</Badge>}
              {selected.translations?.length ? (
                <details className="text-xs leading-relaxed">
                  <summary className="cursor-pointer font-medium">中文名称及来源</summary>
                  <div className="mt-3 flex flex-col gap-4">
                    {selected.translations.map((translation, index) => (
                      <div key={`${translation.provider}:${translation.label}:${index}`} className="flex flex-col gap-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium">{translation.label}</span>
                          <Badge variant="outline">{TRANSLATION_STATUS[translation.status]}</Badge>
                        </div>
                        <p className="text-muted-foreground">提供者：{translation.provider} · 版本：{translation.source_version || "未提供"}</p>
                        <p className="text-muted-foreground">来源语种：{translation.language}</p>
                        <p className="break-words text-muted-foreground">对应依据：{translation.match_basis || "未提供"}</p>
                        {/^https?:\/\//i.test(translation.source_url) ? (
                          <a href={translation.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary underline">
                            {translation.status === "project_draft" ? "译名对应的英文来源" : "中文名称来源"}<ExternalLink className="size-3" />
                          </a>
                        ) : <p className="text-muted-foreground">未提供外部来源链接</p>}
                      </div>
                    ))}
                  </div>
                </details>
              ) : null}
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
                <h3 className="mb-2 font-semibold">父级分类</h3>
                <div className="flex flex-wrap gap-2">
                  {selected.parents.length ? selected.parents.map((identifier) =>
                    identifier.startsWith(`${namespace}:`) ? (
                      <Button key={identifier} variant="outline" onClick={() => browse(identifier)}>
                        {identifier}
                      </Button>
                    ) : <span key={identifier}>{identifier}（外部本体）</span>
                  ) : "来源未提供"}
                </div>
              </div>
              <div>
                <h3 className="mb-2 font-semibold">交叉引用（需核对关系）</h3>
                <p className="break-words leading-relaxed">
                  {selected.cross_references.join(" · ") || "来源未提供"}
                </p>
              </div>
              {namespace === "RXNORM" && (
                <DrugResourceDetails
                  key={`${selected.identifier}:${data?.release?.id ?? releaseId}`}
                  identifier={selected.identifier}
                  releaseId={data?.release?.id ?? releaseId}
                />
              )}
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
