import { useLayoutEffect, useRef, useSyncExternalStore } from "react";

export const ROUTES = {
  "/": { page: "home-root", title: "生信与医学知识库" },
  "/knowledge/diseases/": { page: "standards-root", title: "疾病" },
  "/knowledge/cancers/": { page: "standards-root", title: "癌种分类" },
  "/knowledge/drugs/": { page: "standards-root", title: "药物" },
  "/knowledge/search/": { page: "knowledge-search-root", title: "知识检索" },
  "/knowledge/candidates/": { page: "knowledge-candidates-root", title: "知识候选" },
  "/knowledge/entities/": { page: "entity-registry-root", title: "实体库与名称对齐" },
  "/knowledge/standards/": { page: "standards-root", title: "标准实体" },
  "/evidence/clinical/": { page: "clinical-evidence-root", title: "临床证据" },
  "/knowledge/evidence/": { page: "clinical-evidence-root", title: "临床证据" },
  "/evidence/sources/": { page: "library-root", title: "文本资料" },
  "/evidence/sources/papers/": { page: "library-root", title: "文献管理" },
  "/evidence/sources/guidelines/": { page: "library-root", title: "指南管理" },
  "/library/": { page: "library-root", title: "文本资料" },
  "/management/workflows/": { page: "workflows-root", title: "工作流" },
  "/workflows/": { page: "workflows-root", title: "工作流" },
  "/management/research/": { page: "research-root", title: "研究历程" },
  "/tools/annotate/": { page: "annotate-root", title: "变异注释" },
  "/tools/jobs/": { page: "jobs-root", title: "任务历史" },
  "/tools/engines/": { page: "engines-root", title: "引擎与设置" },
  "/tools/data-updates/": { page: "data-updates-root", title: "数据更新" },
  "/knowledge/genes/": { page: "gene-knowledge-root", title: "基因" },
  "/knowledge/amino-acids/": { page: "amino-acid-knowledge-root", title: "氨基酸映射" },
  "/tools/teaching/": { page: "evidence-root", title: "教学验证" },
  "/knowledge/issues/": { page: "issues-root", title: "问题与经验" },
  "/docs/": { page: "docs-root", title: "API 说明" },
} as const;

export function routeFor(pathname: string) {
  return ROUTES[pathname as keyof typeof ROUTES];
}

const INDEX = "__biomedNavigationIndex";
const listeners = new Set<() => void>();
const blockers = new Map<symbol, string>();
const positions = new Map<number, [number, number]>();
let index = 0;
let revision = 0;
let initialized = false;
let reverting = false;
let snapshot = { url: new URL(location.href), pageKey: "initial", scroll: null as [number, number] | null };
let preload: ((page: string) => void) | undefined;

function publish(remount: boolean, scroll: [number, number] | null = null) {
  snapshot = { url: new URL(location.href), pageKey: remount ? String(++revision) : snapshot.pageKey, scroll };
  listeners.forEach((listener) => listener());
}

function stateWithIndex(state: unknown, value: number) {
  return { ...(state && typeof state === "object" ? state : {}), [INDEX]: value };
}

function canLeave() {
  const message = blockers.values().next().value;
  return !message || window.confirm(`${message}\n离开此页会丢失这些未保存内容，确定离开吗？`);
}

/** Preserve native behavior for external, modified, download and unregistered links. */
function linkUrl(event: MouseEvent | FocusEvent): URL | null {
  const anchor = event.target instanceof Element ? event.target.closest("a") : null;
  if (!anchor || anchor.hasAttribute("download") || anchor.hasAttribute("data-native-navigation")
    || (anchor.target && anchor.target !== "_self") || anchor.relList.contains("external")) return null;
  const href = anchor.getAttribute("href");
  if (!href) return null;
  const url = new URL(href, location.href);
  if (url.origin !== location.origin || !routeFor(url.pathname)) return null;
  return url;
}

export function navigate(href: string) {
  const url = new URL(href, location.href);
  if (url.origin !== location.origin || !routeFor(url.pathname)) {
    location.assign(url.href);
    return;
  }
  if (url.href === location.href) return;
  if (!canLeave()) return;
  positions.set(index, [window.scrollX, window.scrollY]);
  history.pushState(stateWithIndex(null, ++index), "", url);
  publish(true, [0, 0]);
}

export function startNavigation(loadPage: (page: string) => void) {
  if (initialized) return;
  initialized = true;
  preload = loadPage;
  index = typeof history.state?.[INDEX] === "number" ? history.state[INDEX] : 0;
  const replace = history.replaceState.bind(history);
  replace(stateWithIndex(history.state, index), "");
  // Existing pages update their own filters with replaceState. Preserve both their
  // local component state and the index required to cancel a browser Back safely.
  history.replaceState = (state, unused, url) => {
    replace(stateWithIndex(state, index), unused, url);
    publish(false);
  };
  history.scrollRestoration = "manual";
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const url = linkUrl(event);
    if (!url) return;
    // Native anchors within this document keep their normal scrolling/history.
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    event.preventDefault();
    navigate(url.href);
  });
  const warm = (event: MouseEvent | FocusEvent) => {
    const url = linkUrl(event);
    if (url) preload?.(routeFor(url.pathname).page);
  };
  document.addEventListener("pointerover", warm);
  document.addEventListener("focusin", warm);
  window.addEventListener("popstate", (event) => {
    const nextIndex = event.state?.[INDEX];
    if (reverting) {
      reverting = false;
      return;
    }
    const changedPage = snapshot.url.pathname !== location.pathname || snapshot.url.search !== location.search;
    if (changedPage && !canLeave()) {
      if (typeof nextIndex === "number" && nextIndex !== index) {
        reverting = true;
        history.go(index - nextIndex);
      } else {
        // Only foreign same-document entries can lack an index. Keep the draft
        // and restore this URL rather than silently discarding its state.
        replace(stateWithIndex(history.state, index), "", snapshot.url);
      }
      return;
    }
    positions.set(index, [window.scrollX, window.scrollY]);
    if (typeof nextIndex === "number") index = nextIndex;
    if (!routeFor(location.pathname)) {
      location.reload();
      return;
    }
    publish(changedPage, positions.get(index) ?? [0, 0]);
  });
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useNavigation() {
  return useSyncExternalStore(subscribe, () => snapshot);
}

/** One guard covers client navigation, browser history and full-page unloads. */
export function useUnsavedChanges(dirty: boolean, message: string) {
  const id = useRef(Symbol("navigation-guard"));
  useLayoutEffect(() => {
    if (!dirty) return;
    const key = id.current;
    blockers.set(key, message);
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => {
      blockers.delete(key);
      window.removeEventListener("beforeunload", beforeUnload);
    };
  }, [dirty, message]);
}
