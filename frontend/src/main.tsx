import { Component, lazy, StrictMode, Suspense, useLayoutEffect, type ReactNode } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { PersistentAppShell } from "@/layouts/AppShell"
import { routeFor, startNavigation, useNavigation } from "@/lib/navigation"

// Only the active page loads. Hover/focus warms local chunks, not business data.
const loaders = {
  "standards-root": () => import("./pages/StandardsApp"),
  "library-root": () => import("./pages/LibraryApp"),
  "workflows-root": () => import("./pages/WorkflowsApp"),
  "research-root": () => import("./pages/ResearchHistoryApp"),
  "knowledge-search-root": () => import("./pages/KnowledgeSearchApp"),
  "knowledge-candidates-root": () => import("./pages/KnowledgeCandidatesApp"),
  "entity-registry-root": () => import("./pages/EntityRegistryApp"),
  "clinical-evidence-root": () => import("./pages/ClinicalEvidenceApp"),
  "home-root": () => import("./pages/HomeApp"),
  "annotate-root": () => import("./pages/AnnotateApp"),
  "jobs-root": () => import("./pages/JobsApp"),
  "engines-root": () => import("./pages/EnginesApp"),
  "docs-root": () => import("./pages/DocsApp"),
  "data-updates-root": () => import("./pages/DataUpdatesApp"),
  "gene-knowledge-root": () => import("./pages/GeneKnowledgeApp"),
  "amino-acid-knowledge-root": () => import("./pages/AminoAcidKnowledgeApp"),
  "evidence-root": () => import("./pages/EvidenceApp"),
  "issues-root": () => import("./pages/IssuesApp"),
}
type PageId = keyof typeof loaders
const imports = new Map<string, ReturnType<(typeof loaders)[PageId]>>()
function loadPage(page: PageId) {
  let promise = imports.get(page)
  if (!promise) {
    promise = loaders[page]()
    imports.set(page, promise)
    void promise.catch(() => imports.delete(page))
  }
  return promise
}
const pages = Object.fromEntries(Object.keys(loaders).map((id) => [id, lazy(() => loadPage(id as PageId))]))

class PageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() {
    return this.state.failed ? <div role="alert" className="flex flex-col gap-3 text-sm">
      <p>当前页面未能加载。已有任务与数据仍保留，可切换菜单或重新打开此页。</p>
      <a className="underline" href={location.href} data-native-navigation>重新加载此页</a>
    </div> : this.props.children
  }
}

export function RestoreScroll({ pageKey, scroll, hash }: { pageKey: string; scroll: [number, number] | null; hash: string }) {
  useLayoutEffect(() => {
    if (!scroll && !hash) return
    const frame = requestAnimationFrame(() => {
      let target: HTMLElement | null = null
      try { if (hash) target = document.getElementById(decodeURIComponent(hash.slice(1))) } catch { /* invalid fragment keeps normal page scroll */ }
      if (target) target.scrollIntoView()
      else if (scroll) window.scrollTo(...scroll)
    })
    return () => cancelAnimationFrame(frame)
  }, [pageKey, scroll, hash])
  return null
}

export function Application() {
  const navigation = useNavigation()
  const route = routeFor(navigation.url.pathname)
  const Page = pages[route.page]
  return <PersistentAppShell routeKey={navigation.pageKey} title={route.title}>
    <PageBoundary key={navigation.pageKey}>
      <Suspense fallback={<p className="py-8 text-sm text-muted-foreground" role="status">正在打开页面…</p>}>
        <Page />
        <RestoreScroll pageKey={navigation.pageKey} scroll={navigation.scroll} hash={navigation.url.hash} />
      </Suspense>
    </PageBoundary>
  </PersistentAppShell>
}

const root = Object.keys(loaders).map((id) => document.getElementById(id)).find(Boolean)
if (root && routeFor(location.pathname)) {
  startNavigation((page) => { void loadPage(page as PageId).catch(() => undefined) })
  createRoot(root).render(<StrictMode><TooltipProvider><Application /><Toaster /></TooltipProvider></StrictMode>)
}
