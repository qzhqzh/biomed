import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ChevronDown, ChevronRight, FolderTree, LoaderCircle, Search } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, FieldGroup } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { ErrorMessage, NoData } from "@/components/curation/Common";
import { TranslationLegend } from "@/components/curation/TranslationLegend";
import { curation, type Concept, type Release } from "@/lib/curation";
import type { DrugClass, DrugTreePage } from "@/lib/drug-tree";
import { preferredChinese, TRANSLATION_STATUS } from "@/lib/standard-labels";

type Branch = { nodes: Concept[]; total: number; loaded: boolean; loading: boolean; error: string };
const emptyBranch = (): Branch => ({ nodes: [], total: 0, loaded: false, loading: false, error: "" });

function Name({ node }: { node: Pick<Concept, "identifier" | "label" | "translations"> }) {
  const chinese = preferredChinese(node);
  return <div className="min-w-0 flex-1 [overflow-wrap:anywhere]">
    <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
      <span>{chinese?.label ?? node.label}</span>
      {chinese && <Badge variant="outline" className="text-[10px] font-normal">{TRANSLATION_STATUS[chinese.status]}</Badge>}
    </div>
    {chinese && <p className="mt-0.5 text-xs text-muted-foreground">{node.label}</p>}
    <p className="mt-1 text-xs text-muted-foreground">{node.identifier}{!chinese && " · 中文待补充"}</p>
  </div>;
}

export function DrugTreeBrowser({ release, initialQuery = "", onSelect }: {
  release: Release;
  initialQuery?: string;
  onSelect: (node: Concept) => void;
}) {
  const [input, setInput] = useState(initialQuery);
  const [query, setQuery] = useState(initialQuery.trim());
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState<DrugTreePage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const cache = useRef<Record<string, Branch>>({});
  const [branches, setBranches] = useState(cache.current);
  const pinnedDataset = useRef<string | null>(null);
  const alive = useRef(true);
  const controllers = useRef(new Set<AbortController>());
  const pending = useRef(new Map<string, Promise<void>>());
  const listHeading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    alive.current = true;
    const requests = controllers.current;
    const inflight = pending.current;
    return () => {
      alive.current = false;
      requests.forEach((controller) => controller.abort());
      requests.clear();
      inflight.clear();
    };
  }, []);

  const request = useCallback(async (params: Record<string, string>, signal: AbortSignal) => {
    const dataset = pinnedDataset.current;
    const result = await curation<DrugTreePage>(`drug-tree/?${new URLSearchParams({
      release: release.id, ...(dataset ? { dataset } : {}), ...params,
    })}`, { signal });
    if (result.release.id !== release.id || (dataset && result.dataset?.id !== dataset)) {
      throw new Error("来源版本发生变化，请重新打开药物页面。");
    }
    return result;
  }, [release.id]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setPage(null);
    void request({ q: query, offset: String(offset), limit: "50" }, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;
        if (result.dataset) pinnedDataset.current = result.dataset.id;
        setPage(result);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "分类加载失败");
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [request, query, offset, revision]);

  const branchKey = (identifier: string) => `${query}\u0000${identifier}`;
  const loadBranch = (node: DrugClass, more = false): Promise<void> => {
    const key = branchKey(node.identifier);
    const inflight = pending.current.get(key);
    if (inflight) return inflight;
    const previous = cache.current[key] ?? emptyBranch();
    if (previous.loaded && (!more || previous.nodes.length >= previous.total)) return Promise.resolve();
    const save = (branch: Branch) => {
      if (!alive.current) return;
      cache.current = { ...cache.current, [key]: branch };
      setBranches(cache.current);
    };
    save({ ...previous, loading: true, error: "" });
    const controller = new AbortController();
    controllers.current.add(controller);
    const promise = request({ class_id: node.identifier, q: query, offset: String(previous.nodes.length), limit: "50" }, controller.signal)
      .then((result) => {
        if (result.kind !== "ingredients" || result.class?.identifier !== node.identifier) {
          throw new Error("分类响应不一致，请重试。");
        }
        const rows = result.results as Concept[];
        const nodes = [...previous.nodes, ...rows];
        if (new Set(nodes.map((item) => item.identifier)).size !== nodes.length || (!rows.length && nodes.length < result.total)) {
          throw new Error("成分页码不连续，请重试。");
        }
        save({ nodes, total: result.total, loaded: true, loading: false, error: "" });
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) save({ ...previous, loading: false, error: reason instanceof Error ? reason.message : "成分加载失败" });
      })
      .finally(() => {
        controllers.current.delete(controller);
        if (pending.current.get(key) === promise) pending.current.delete(key);
      });
    pending.current.set(key, promise);
    return promise;
  };

  const toggle = (node: DrugClass) => {
    const opened = expanded.has(node.identifier);
    setExpanded((previous) => {
      const next = new Set(previous);
      if (opened) next.delete(node.identifier); else next.add(node.identifier);
      return next;
    });
    if (!opened) void loadBranch(node);
  };

  const search = (event: FormEvent) => {
    event.preventDefault();
    setQuery(input.trim());
    setOffset(0);
    setExpanded(new Set());
  };

  const changePage = (next: number) => {
    setOffset(next);
    listHeading.current?.focus();
  };

  return <div className="flex flex-col gap-4">
    <form onSubmit={search}><FieldGroup><Field orientation="horizontal">
      <Input aria-label="搜索药理分类或成分" placeholder="搜索分类或成分的中文、英文、编号" value={input} onChange={(event) => setInput(event.target.value)} maxLength={200} />
      <Button type="submit" variant="outline" aria-label="搜索药物分类"><Search data-icon="inline-start" /><span className="hidden sm:inline">搜索</span></Button>
      {query && <Button type="button" variant="ghost" onClick={() => { setInput(""); setQuery(""); setOffset(0); setExpanded(new Set()); }}>清除</Button>}
    </Field></FieldGroup></form>
    <TranslationLegend />
    <Alert role="note"><AlertDescription className="flex flex-col gap-1">
      <div>EPC 药理分类 → 药物成分。一个药物可属于多个分类；点击箭头展开，只有“详情”会打开药物详情。</div>
      <div>这里展示已收录的直接分类关联。没有出现在树中的成分，仍可在“全部药物成分”中查询。</div>
      {page?.dataset && <div className="[overflow-wrap:anywhere]">RxClass / FDASPL · {page.dataset.version} · RxNorm {release.version}</div>}
    </AlertDescription></Alert>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 ref={listHeading} tabIndex={-1} className="flex items-center gap-2 text-sm font-medium outline-none"><FolderTree className="size-4" />药理分类树</h3>
      <Button size="sm" variant="ghost" disabled={!expanded.size} onClick={() => setExpanded(new Set())}>全部折叠</Button>
    </div>
    <ErrorMessage error={error} />
    {error && <Button variant="outline" size="sm" onClick={() => setRevision((value) => value + 1)}>重试加载</Button>}
    {loading && <p role="status" className="flex items-center gap-2 py-8 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />正在加载药理分类…</p>}
    {!loading && page?.state === "waiting_dataset" && <div className="flex flex-col gap-3">
      <NoData title="药理分类正在准备" description="RxClass 分类及关联完成采集和校验后会启用，当前不会把部分结果当作完整分类。可先在“全部药物成分”中查看药物。" />
      <Button variant="outline" size="sm" onClick={() => setRevision((value) => value + 1)}>检查采集状态</Button>
    </div>}
    {!loading && page?.state === "ready" && <>
      <p className="text-xs text-muted-foreground" aria-live="polite">{query ? `找到 ${page.total.toLocaleString()} 个匹配分类` : `共 ${page.total.toLocaleString()} 个分类`}；已关联 {page.coverage?.classified_ingredients.toLocaleString()} / {page.coverage?.local_ingredients.toLocaleString()} 个本地成分</p>
      {page.total === 0 && <NoData title="没有匹配分类" description="可换用英文名或编号；尚无 EPC 关联的药物请到“全部药物成分”中搜索。" />}
      <ul aria-label="药理分类树" className="divide-y rounded-lg border">
        {(page.results as DrugClass[]).map((node) => {
          const opened = expanded.has(node.identifier);
          const branch = branches[branchKey(node.identifier)] ?? emptyBranch();
          const chinese = preferredChinese(node);
          const name = chinese?.label ?? node.label;
          const childId = `drug-class-${node.identifier}`;
          return <li key={node.identifier} className="px-2 py-3 sm:px-3">
            <div className="flex items-start gap-2">
              {node.child_count > 0 ? <Button variant="ghost" size="icon-lg" aria-expanded={opened} aria-controls={childId} aria-label={`${opened ? "折叠" : "展开"}${name}`} onClick={() => toggle(node)}>
                {opened ? <ChevronDown /> : <ChevronRight />}
              </Button> : <span className="size-8 shrink-0" />}
              <Name node={node} />
              <Badge variant="secondary" className="mt-1 shrink-0">{node.child_count} 成分</Badge>
            </div>
            {node.unavailable_count > 0 && <p className="mt-1 pl-10 text-xs text-muted-foreground">另有 {node.unavailable_count} 个来源成分不在当前 RxNorm 版本中</p>}
            <div id={childId} hidden={!opened}>{opened && <div className="ml-4 mt-3 flex flex-col gap-3 border-l pl-3 sm:ml-4 sm:pl-5">
              {query && branch.loaded && branch.total < node.child_count && <p className="text-xs text-muted-foreground">此分类中匹配 {branch.total} / {node.child_count} 个成分</p>}
              {branch.nodes.length > 0 && <ul aria-label={`${name}的药物成分`} className="flex flex-col gap-3">
                {branch.nodes.map((drug) => <li key={drug.identifier} className="flex items-start gap-2 rounded-md bg-muted/20 px-2 py-2.5 sm:px-3">
                  <Name node={drug} />
                  <Button variant="outline" size="sm" className="shrink-0" onClick={() => onSelect(drug)} aria-label={`查看${preferredChinese(drug)?.label ?? drug.label}详情`}>详情</Button>
                </li>)}
              </ul>}
              {branch.loading && <p role="status" className="flex items-center gap-2 text-xs text-muted-foreground"><LoaderCircle className="size-3 animate-spin" />正在加载成分…</p>}
              <ErrorMessage error={branch.error} />
              {branch.error && <Button size="sm" variant="outline" onClick={() => { void loadBranch(node, true); }}>重试</Button>}
              {branch.loaded && !branch.total && <p className="text-xs text-muted-foreground">当前筛选条件下没有成分。</p>}
              {branch.loaded && branch.nodes.length < branch.total && !branch.error && <Button size="sm" variant="outline" disabled={branch.loading} onClick={() => { void loadBranch(node, true); }}>加载更多成分（{branch.nodes.length} / {branch.total}）</Button>}
            </div>}</div>
          </li>;
        })}
      </ul>
      {page.total > 50 && <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>{offset + 1}–{Math.min(offset + 50, page.total)} / {page.total}</span>
        <div className="flex gap-2"><Button size="sm" variant="outline" disabled={offset === 0} onClick={() => changePage(Math.max(0, offset - 50))}>上一页</Button><Button size="sm" variant="outline" disabled={offset + 50 >= page.total} onClick={() => changePage(offset + 50)}>下一页</Button></div>
      </div>}
    </>}
  </div>;
}
