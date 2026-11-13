import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ChevronDown, ChevronRight, FolderTree, LoaderCircle, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { ErrorMessage, NoData } from "@/components/curation/Common";
import { TranslationLegend } from "@/components/curation/TranslationLegend";
import { curation, useCuration, type Concept, type Release } from "@/lib/curation";
import { preferredChinese, TRANSLATION_STATUS } from "@/lib/standard-labels";
import { cn } from "@/lib/utils";

const ROOT = "ONCOTREE:TISSUE";
type Page = { release: Release; results: Concept[]; total: number; tree_path?: string[] };
type Branch = { nodes: Concept[]; total: number; loaded: boolean; loading: boolean; error: string };
const emptyBranch = (): Branch => ({ nodes: [], total: 0, loaded: false, loading: false, error: "" });

function NodeName({ node }: { node: Concept }) {
  const chinese = preferredChinese(node);
  return (
    <span className="block min-w-0">
      <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm font-medium [overflow-wrap:anywhere]">
        {chinese?.label ?? node.label}
        {chinese && <Badge variant="outline" className="text-[10px] font-normal">{TRANSLATION_STATUS[chinese.status]}</Badge>}
      </span>
      {chinese && <span className="mt-0.5 block text-xs text-muted-foreground [overflow-wrap:anywhere]">{node.label}</span>}
      <span className="mt-1 block text-xs text-muted-foreground [overflow-wrap:anywhere]">
        {node.identifier}{!chinese && " · 中文待补充"}
      </span>
    </span>
  );
}

export function CancerTreeBrowser({ release, roots, rootTotal = roots.length, initialQuery = "", revealIdentifier, onSelect }: {
  release: Release;
  roots: Concept[];
  rootTotal?: number;
  initialQuery?: string;
  revealIdentifier?: string | null;
  onSelect: (node: Concept) => void;
}) {
  const [input, setInput] = useState(initialQuery);
  const [query, setQuery] = useState(initialQuery.trim());
  const [offset, setOffset] = useState(0);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const cache = useRef<Record<string, Branch>>({
    [ROOT]: { nodes: roots, total: rootTotal, loaded: true, loading: false, error: "" },
  });
  const [branches, setBranches] = useState(cache.current);
  const [locating, setLocating] = useState(false);
  const [locateError, setLocateError] = useState("");
  const [located, setLocated] = useState<{ id: string; serial: number } | null>(null);
  const alive = useRef(true);
  const generation = useRef(0);
  const requests = useRef(new Set<AbortController>());
  const pending = useRef(new Map<string, Promise<Branch>>());
  const nodeRows = useRef(new Map<string, HTMLDivElement>());
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    alive.current = true;
    const controllers = requests.current;
    const inflight = pending.current;
    return () => {
      alive.current = false;
      generation.current += 1;
      controllers.forEach((controller) => controller.abort());
      controllers.clear();
      inflight.clear();
    };
  }, []);

  const request = useCallback(async (params: Record<string, string>) => {
    const controller = new AbortController();
    requests.current.add(controller);
    try {
      const page = await curation<Page>(`concepts/?${new URLSearchParams({
        namespace: "ONCOTREE", release: release.id, ...params,
      })}`, { signal: controller.signal });
      if (page.release?.id !== release.id) throw new Error("来源版本发生变化，请重新打开页面。");
      return page;
    } finally {
      requests.current.delete(controller);
    }
  }, [release.id]);

  const loadBranch = useCallback((identifier: string, more = false): Promise<Branch> => {
    const inflight = pending.current.get(identifier);
    if (inflight) return inflight;
    const previous = cache.current[identifier] ?? emptyBranch();
    if (previous.loaded && (!more || previous.nodes.length >= previous.total)) return Promise.resolve(previous);
    const save = (branch: Branch) => {
      if (!alive.current) return;
      cache.current = { ...cache.current, [identifier]: branch };
      setBranches(cache.current);
    };
    save({ ...previous, loading: true, error: "" });
    const promise = request({ parent: identifier, offset: String(previous.nodes.length), limit: "50" })
      .then((page) => {
        const nodes = [...previous.nodes, ...page.results];
        if (new Set(nodes.map((node) => node.identifier)).size !== nodes.length ||
          (!page.results.length && nodes.length < page.total)) {
          throw new Error("分类分页结果不完整，请重试。");
        }
        const branch = { nodes, total: page.total, loaded: true, loading: false, error: "" };
        save(branch);
        return branch;
      })
      .catch((error: Error) => {
        if (error.name !== "AbortError") save({ ...previous, loading: false, error: error.message });
        throw error;
      })
      .finally(() => {
        if (pending.current.get(identifier) === promise) pending.current.delete(identifier);
      });
    pending.current.set(identifier, promise);
    return promise;
  }, [request]);

  const cancelLocate = () => {
    generation.current += 1;
    setLocating(false);
    setLocateError("");
  };

  const locate = useCallback(async (identifier: string) => {
    const serial = ++generation.current;
    setLocating(true);
    setLocateError("");
    try {
      const result = await request({ tree_path: identifier, parent: ROOT, limit: "1" });
      const path = result.tree_path;
      if (!path?.length || path[0] !== ROOT || path.at(-1) !== identifier) {
        throw new Error("此节点的分类路径不完整。");
      }
      for (let i = 0; i < path.length - 1; i += 1) {
        if (generation.current !== serial) return;
        let branch = await loadBranch(path[i]);
        while (!branch.nodes.some((node) => node.identifier === path[i + 1]) && branch.nodes.length < branch.total) {
          if (generation.current !== serial) return;
          branch = await loadBranch(path[i], true);
        }
        if (!branch.nodes.some((node) => node.identifier === path[i + 1])) throw new Error("未找到路径中的子节点，请重试。");
      }
      if (!alive.current || generation.current !== serial) return;
      setExpanded((previous) => new Set([...previous, ...path.slice(1, -1)]));
      setInput("");
      setQuery("");
      setOffset(0);
      setLocated({ id: identifier, serial });
    } catch (error) {
      if (alive.current && generation.current === serial && (error as Error).name !== "AbortError") {
        setLocateError((error as Error).message);
      }
    } finally {
      if (alive.current && generation.current === serial) setLocating(false);
    }
  }, [request, loadBranch]);

  useEffect(() => {
    if (revealIdentifier) void locate(revealIdentifier);
  }, [revealIdentifier, locate]);

  useEffect(() => {
    if (!located || query) return;
    const frame = requestAnimationFrame(() => {
      const element = located.id === ROOT ? heading.current : nodeRows.current.get(located.id);
      element?.focus({ preventScroll: true });
      element?.scrollIntoView({ block: "center" });
    });
    return () => cancelAnimationFrame(frame);
  }, [located, query]);

  const searchPath = query ? `concepts/?${new URLSearchParams({
    namespace: "ONCOTREE", release: release.id, q: query, offset: String(offset), limit: "50",
  })}` : null;
  const search = useCuration<Page>(searchPath);

  function submit(event: FormEvent) {
    event.preventDefault();
    cancelLocate();
    setQuery(input.trim());
    setOffset(0);
  }

  function toggle(node: Concept) {
    cancelLocate();
    const open = !expanded.has(node.identifier);
    setExpanded((previous) => {
      const next = new Set(previous);
      if (open) next.add(node.identifier);
      else next.delete(node.identifier);
      return next;
    });
    if (open) void loadBranch(node.identifier).catch(() => undefined);
  }

  function branchContents(identifier: string) {
    const branch = branches[identifier];
    return (
      <>
        {branch?.nodes.map((node) => {
          const open = expanded.has(node.identifier);
          const hasChildren = (node.child_count ?? 0) > 0;
          const name = preferredChinese(node)?.label ?? node.label;
          return (
            <li key={node.identifier}>
              <div role="group" aria-label={name} tabIndex={-1}
                ref={(element) => { if (element) nodeRows.current.set(node.identifier, element); else nodeRows.current.delete(node.identifier); }}
                className={cn("my-0.5 flex items-start gap-1 rounded-md pr-2 outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  located?.id === node.identifier && "bg-primary/5 ring-1 ring-inset ring-primary/30")}>
                {hasChildren ? (
                  <Button variant="ghost" size="icon" className="mt-2 size-9 shrink-0" type="button"
                    aria-label={`${open ? "折叠" : "展开"}${name}`} aria-expanded={open}
                    aria-controls={`children-${node.identifier}`} onClick={() => toggle(node)}>
                    {open ? <ChevronDown /> : <ChevronRight />}
                  </Button>
                ) : <span aria-hidden="true" className="flex size-9 shrink-0 items-center justify-center mt-2"><span className="size-1 rounded-full bg-muted-foreground/50" /></span>}
                <div className="min-w-0 flex-1 py-3">
                  <NodeName node={node} />
                  {hasChildren && <span className="mt-1 block text-xs text-muted-foreground" title="直接子类数量">{node.child_count} 个直接子类</span>}
                </div>
                <Button type="button" variant="outline" className="mt-2 h-9" aria-label={`查看${name}详情`}
                  onClick={() => onSelect(node)}>详情</Button>
              </div>
              {hasChildren && <ul id={`children-${node.identifier}`} aria-label={`${name}的子类`} hidden={!open}
                className="ml-4 border-l pl-2 sm:ml-5 sm:pl-3">{open && branchContents(node.identifier)}</ul>}
            </li>
          );
        })}
        {(!branch || branch.loading) && <li role="status" className="flex items-center gap-2 px-3 py-3 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />读取子类…</li>}
        {branch?.error && <li className="flex flex-wrap items-center gap-2 px-3 py-3 text-sm">
          <span role="alert" className="text-destructive">{branch.error}</span>
          <Button variant="outline" onClick={() => void loadBranch(identifier, true).catch(() => undefined)}>重试读取子类</Button>
        </li>}
        {branch?.loaded && branch.nodes.length < branch.total && !branch.loading && !branch.error && <li className="px-3 py-2">
          <Button variant="outline" onClick={() => void loadBranch(identifier, true).catch(() => undefined)}>加载更多子类（{branch.nodes.length}/{branch.total}）</Button>
        </li>}
      </>
    );
  }

  return (
    <section aria-label="癌种分类浏览" className="flex min-w-0 flex-col gap-4">
      <form onSubmit={submit} className="flex items-end gap-2">
        <div className="min-w-0 flex-1 space-y-2">
          <label htmlFor="cancer-query" className="text-sm font-medium">搜索全部癌种分类</label>
          <Input id="cancer-query" className="h-10" value={input} onChange={(event) => setInput(event.target.value)}
            placeholder="中文、英文或代码，例如 肺腺癌、LUAD" />
        </div>
        <Button type="submit" className="h-10 px-4 text-sm"><Search />查询</Button>
      </form>
      <TranslationLegend />
      <ErrorMessage error={locateError || search.error} />
      {locating && <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />正在展开分类路径…</p>}
      {query ? (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p role="status" className="text-sm text-muted-foreground">{search.loading ? "正在搜索…" : search.data ? `“${query}” · ${search.data.total} 个匹配节点` : "搜索未完成"}</p>
            <Button variant="outline" onClick={() => { cancelLocate(); setInput(""); setQuery(""); setOffset(0); }}>返回分类树</Button>
          </div>
          {search.error && <Button variant="outline" onClick={search.refresh}>重试搜索</Button>}
          {search.data?.total === 0 && <NoData title="没有匹配的分类" description="可尝试英文名称或 OncoTree 代码；部分亚型的中文仍待补充。" />}
          <ul aria-label="癌种搜索结果" className="divide-y rounded-lg border px-3">
            {search.data?.results.map((node) => <li key={node.identifier} className="flex flex-wrap items-center gap-3 py-3">
              <div className="min-w-0 flex-1 basis-48"><NodeName node={node} /></div>
              <div className="flex shrink-0 items-center gap-2">
                <Button type="button" variant="outline" className="h-9" aria-label={`查看${preferredChinese(node)?.label ?? node.label}详情`}
                  onClick={() => onSelect(node)}>详情</Button>
                <Button type="button" variant="outline" className="h-9" disabled={locating} onClick={() => void locate(node.identifier)}><FolderTree data-icon="inline-start" />在树中定位</Button>
              </div>
            </li>)}
          </ul>
          {search.data && search.data.total > 50 && <div className="flex items-center gap-3">
            <Button variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button>
            <span className="text-xs text-muted-foreground">{offset + 1}–{offset + search.data.results.length} / {search.data.total}</span>
            <Button variant="outline" disabled={offset + search.data.results.length >= search.data.total} onClick={() => setOffset(offset + 50)}>下一页</Button>
          </div>}
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b bg-muted/30 p-4">
            <div className="space-y-1">
              <h2 ref={heading} tabIndex={-1} className="flex items-center gap-2 text-sm font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring"><FolderTree className="size-4 text-primary" />组织与癌种分类树</h2>
              <p className="text-xs text-muted-foreground">点箭头展开子类；仅“详情”按钮打开详情与来源。</p>
            </div>
            <Button variant="outline" disabled={expanded.size === 0 && !locating} onClick={() => { cancelLocate(); setExpanded(new Set()); setLocated(null); }}>全部折叠</Button>
          </div>
          <ul aria-label="癌种分类树" className="p-2 sm:p-3">{branchContents(ROOT)}</ul>
        </div>
      )}
    </section>
  );
}
