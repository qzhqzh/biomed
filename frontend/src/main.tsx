import { lazy, StrictMode, Suspense } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"

// Load only the current page. In particular, gene/evidence lookup needs no Monaco editor.
const pages = {
  "standards-root": lazy(() => import("./pages/StandardsApp")),
  "library-root": lazy(() => import("./pages/LibraryApp")),
  "workflows-root": lazy(() => import("./pages/WorkflowsApp")),
  "clinical-evidence-root": lazy(() => import("./pages/ClinicalEvidenceApp")),
  "home-root": lazy(() => import("./pages/HomeApp")),
  "annotate-root": lazy(() => import("./pages/AnnotateApp")),
  "jobs-root": lazy(() => import("./pages/JobsApp")),
  "engines-root": lazy(() => import("./pages/EnginesApp")),
  "docs-root": lazy(() => import("./pages/DocsApp")),
  "data-updates-root": lazy(() => import("./pages/DataUpdatesApp")),
  "gene-knowledge-root": lazy(() => import("./pages/GeneKnowledgeApp")),
  "amino-acid-knowledge-root": lazy(() => import("./pages/AminoAcidKnowledgeApp")),
  "evidence-root": lazy(() => import("./pages/EvidenceApp")),
  "issues-root": lazy(() => import("./pages/IssuesApp")),
}
for (const [id, Page] of Object.entries(pages)) {
  const root = document.getElementById(id)
  if (!root) continue
  createRoot(root).render(<StrictMode><TooltipProvider>
    <Suspense fallback={<p className="p-8 text-sm text-muted-foreground" role="status">正在打开 biomed…</p>}><Page /></Suspense>
    <Toaster />
  </TooltipProvider></StrictMode>)
}
