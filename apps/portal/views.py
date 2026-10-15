from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie


@ensure_csrf_cookie
def data_updates_page(request):
    return render(request, "portal/data_updates.html")


def home(request):
    """Portal home; JSON clients get service metadata."""
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse(
            {
                "service": "biomed",
                "evidence_ui": "/knowledge/evidence/",
                "issues_ui": "/knowledge/issues/",
                "docs": "/docs/",
                "annotate_ui": "/tools/annotate/",
                "jobs_ui": "/tools/jobs/",
                "engines_ui": "/tools/engines/",
                "gene_knowledge_ui": "/knowledge/genes/",
                "amino_acid_knowledge_ui": "/knowledge/amino-acids/",
            }
        )
    return render(request, "portal/home.html")


def annotate_tool(request):
    return render(request, "portal/annotate.html")


def jobs_page(request):
    return render(request, "portal/jobs.html")


def engines_page(request):
    return render(request, "portal/engines.html")


def docs_page(request):
    return render(request, "portal/docs.html")


def gene_knowledge_page(request):
    return render(request, "portal/gene_knowledge.html")


def amino_acid_knowledge_page(request):
    return render(request, "portal/amino_acid_knowledge.html")


@ensure_csrf_cookie
def evidence_page(request):
    return render(request, "portal/evidence.html")


@ensure_csrf_cookie
def issues_page(request):
    return render(request, "portal/issues.html")


@ensure_csrf_cookie
def standards_page(request, namespace=None):
    namespace = namespace or request.GET.get("namespace", "MONDO")
    title = "药物" if namespace == "RXNORM" else "疾病"
    return render(request, "portal/standards.html", {"page_title": title})


@ensure_csrf_cookie
def library_page(request, kind=None):
    kind = kind or request.GET.get("kind")
    title = {"paper": "文献管理", "guideline": "指南管理"}.get(kind, "文本资料")
    return render(request, "portal/library.html", {"page_title": title})


@ensure_csrf_cookie
def workflows_page(request):
    return render(request, "portal/workflows.html")


@ensure_csrf_cookie
def clinical_evidence_page(request):
    return render(request, "portal/clinical_evidence.html")
