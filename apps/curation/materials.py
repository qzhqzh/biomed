"""PubMed metadata → licensed PMC full text → immutable, citable passages."""

import io
import re
from pathlib import Path
from urllib.parse import urlencode
from xml.etree.ElementTree import tostring

from django.db import transaction

from apps.curation.models import Material, MaterialVersion, Passage
from apps.curation.network import SourceBlocked, fetch, fetch_json, text, xml
from apps.curation.storage import read_blob, store_blob

PARSER_VERSION = "text-v1-pypdf-6.20.0-jats-1"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"


def pubmed_search(query):
    search = fetch_json(
        EUTILS
        + "esearch.fcgi?"
        + urlencode(
            {
                "db": "pubmed",
                "retmode": "json",
                "retmax": 10,
                "term": query,
                "sort": "relevance",
                "tool": "biomed",
            }
        )
    )
    ids = search.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return {
            "total": int(search.get("esearchresult", {}).get("count", 0)),
            "articles": [],
        }
    data = fetch_json(
        EUTILS
        + "esummary.fcgi?"
        + urlencode(
            {"db": "pubmed", "retmode": "json", "id": ",".join(ids), "tool": "biomed"}
        )
    )["result"]
    return {
        "total": int(search["esearchresult"]["count"]),
        "articles": [
            {
                "pmid": key,
                "title": data[key]["title"],
                "journal": data[key].get("fulljournalname"),
                "date": data[key].get("pubdate"),
            }
            for key in ids
            if key in data
        ],
    }


def pubmed_record_id(article):
    path = {
        "PubmedArticle": "./MedlineCitation/PMID",
        "PubmedBookArticle": "./BookDocument/PMID",
    }.get(article.tag)
    return text(article.find(path)) if path else ""


def pubmed_records(root):
    if root.tag != "PubmedArticleSet":
        raise SourceBlocked("PubMed 未返回可识别的记录集合")
    return [node for node in root if node.tag in ("PubmedArticle", "PubmedBookArticle")]


def pubmed_identifiers(article):
    paths = ["./PubmedData/ArticleIdList/ArticleId"]
    if article.tag == "PubmedBookArticle":
        paths = [
            "./BookDocument/ArticleIdList/ArticleId",
            "./PubmedBookData/ArticleIdList/ArticleId",
        ]
    identifiers = {
        node.attrib.get("IdType"): text(node)
        for path in paths
        for node in article.findall(path)
    }
    identifiers["pubmed"] = pubmed_record_id(article)
    return identifiers


def pubmed_metadata(article):
    if article.tag == "PubmedBookArticle":
        document = article.find("./BookDocument")
        if document is None:
            raise SourceBlocked("PubMed 图书记录缺少 BookDocument")
        return {
            "identifiers": pubmed_identifiers(article),
            "source_parser_version": "pubmed-book-metadata-v1",
            "record_type": "book_chapter",
            "title": text(document.find("./ArticleTitle")),
            "abstract": "\n".join(
                text(n) for n in document.findall("./Abstract/AbstractText")
            ),
            "journal": "",
            "book_title": text(document.find("./Book/BookTitle")),
            # The book's founding date is not the chapter's publication date.
            "publication_date": text(document.find("./PubDate")),
            "book_publication_date": text(document.find("./Book/PubDate")),
            "languages": [text(n) for n in document.findall("./Language")],
            "mesh": [
                text(n)
                for n in document.findall(
                    "./MeshHeadingList/MeshHeading/DescriptorName"
                )
            ],
            "mesh_ids": [
                n.attrib.get("UI", "")
                for n in document.findall(
                    "./MeshHeadingList/MeshHeading/DescriptorName"
                )
            ],
            "publication_types": ["Book Chapter"],
            "keywords": [text(n) for n in document.findall("./KeywordList/Keyword")],
        }
    return {
        "identifiers": pubmed_identifiers(article),
        "source_parser_version": "pubmed-metadata-v3",
        "title": text(article.find("./MedlineCitation/Article/ArticleTitle")),
        "abstract": "\n".join(
            text(n)
            for n in article.findall("./MedlineCitation/Article/Abstract/AbstractText")
        ),
        "journal": text(article.find(".//Journal/Title")),
        "publication_date": text(article.find(".//Journal/JournalIssue/PubDate")),
        "languages": [text(n) for n in article.findall(".//Article/Language")],
        "mesh": [text(n) for n in article.findall(".//MeshHeading/DescriptorName")],
        "mesh_ids": [
            n.attrib.get("UI", "")
            for n in article.findall(".//MeshHeading/DescriptorName")
        ],
        "publication_types": [text(n) for n in article.findall(".//PublicationType")],
        "keywords": [text(n) for n in article.findall(".//Keyword")],
    }


def prepare_pubmed_article(article, content=None):
    """Parse and fsync immutable content before taking the shared database write lock."""
    pmid = pubmed_record_id(article)
    if not re.fullmatch(r"\d{1,10}", pmid):
        raise SourceBlocked("PubMed 返回的 PMID 无效")
    identifiers = pubmed_identifiers(article)
    metadata = pubmed_metadata(article)
    metadata["source_url"] = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    if content is None:
        content = (
            b"<PubmedArticleSet>"
            + tostring(article, encoding="utf-8")
            + b"</PubmedArticleSet>"
        )
    digest = store_blob(content)
    return {
        "pmid": pmid,
        "kind": Material.Kind.OTHER
        if article.tag == "PubmedBookArticle"
        else Material.Kind.PAPER,
        "identifiers": identifiers,
        "metadata": metadata,
        "digest": digest,
    }


def save_prepared_pubmed_article(prepared, url):
    pmid, identifiers, metadata, digest = (
        prepared[key] for key in ("pmid", "identifiers", "metadata", "digest")
    )
    with transaction.atomic():
        material, _ = Material.objects.get_or_create(
            source_key="pubmed:" + pmid,
            defaults={
                "kind": prepared["kind"],
                "title": metadata["title"][:2000],
                "identifiers": identifiers,
                "metadata": metadata,
            },
        )
        version, _ = MaterialVersion.objects.get_or_create(
            material=material,
            sha256=digest,
            defaults={
                "media_type": "application/pubmed+xml",
                "filename": f"pubmed-{pmid}.xml",
                "source_url": url,
                "rights": "PubMed 元数据；原文权利不由此授予。",
            },
        )
        # Metadata changes cannot silently mutate or invalidate previous text/evidence versions.
        material.identifiers = identifiers
        material.metadata = material.metadata | metadata
        material.title = metadata["title"][:2000]
        material.save(update_fields=["identifiers", "metadata", "title"])
    return material, version, metadata


def save_pubmed_article(article, url, content=None):
    """Save an immutable article snapshot; batch responses remain in the fetch ledger."""
    return save_prepared_pubmed_article(prepare_pubmed_article(article, content), url)


def acquire_pubmed(pmid):
    from apps.curation.workflows import enqueue

    if not re.fullmatch(r"\d{1,10}", pmid):
        raise SourceBlocked("请输入有效的 PMID")
    url = (
        EUTILS
        + "efetch.fcgi?"
        + urlencode({"db": "pubmed", "id": pmid, "retmode": "xml", "tool": "biomed"})
    )
    content = fetch(url, 8 * 1024 * 1024)
    records = pubmed_records(xml(content))
    if len(records) != 1 or pubmed_record_id(records[0]) != pmid:
        raise SourceBlocked("PubMed 没有返回指定 PMID")
    article = records[0]
    with transaction.atomic():
        material, _, _ = save_pubmed_article(article, url, content)
        identifiers = material.identifiers
        pmcid = identifiers.get("pmc")
        job = (
            enqueue("acquire_pmc", {"material_id": str(material.pk), "pmcid": pmcid})
            if pmcid
            else None
        )
    return {
        "material_id": str(material.pk),
        "next_job": str(job.pk) if job else None,
        "message": "已保存 PubMed 元数据，继续检查 PMC 全文权限。"
        if pmcid
        else "已保存元数据；未提供 PMC 全文标识，可上传合法持有的 PDF。",
    }


def strip_namespaces(root):
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    return root


def validate_pmc(content, pmcid, identifiers):
    """Validate both cached and downloaded text against the caller's fixed identity."""
    if not re.fullmatch(r"PMC\d{1,12}", pmcid or ""):
        raise SourceBlocked("无效的 PMC 标识")
    root = strip_namespaces(xml(content))
    article = root.find(".//article")
    if article is None or article.find("body") is None:
        raise SourceBlocked(
            "PMC 未提供可复用全文；元数据已保留，可上传合法持有的 PDF。"
        )
    ids = [
        text(n).removeprefix("PMC")
        for n in article.findall("./front/article-meta/article-id")
        if n.attrib.get("pub-id-type") in ("pmc", "pmcid")
    ]
    if pmcid[3:] not in ids:
        raise SourceBlocked("PMC 返回的全文标识与请求不符")
    article_ids = {
        node.attrib.get("pub-id-type"): text(node)
        for node in article.findall("./front/article-meta/article-id")
    }
    expected_pmid = identifiers.get("pubmed")
    expected_doi = identifiers.get("doi")
    if expected_pmid and article_ids.get("pmid") != expected_pmid:
        raise SourceBlocked("PMC 全文的 PMID 与入库论文不一致或缺失，未建立关联")
    if expected_doi and article_ids.get("doi", "").lower() != expected_doi.lower():
        raise SourceBlocked("PMC 全文的 DOI 与入库论文不一致或缺失，未建立关联")
    permissions = article.find(".//article-meta/permissions")
    license_node = permissions.find("license") if permissions is not None else None
    if license_node is None or not text(license_node):
        raise SourceBlocked("全文缺少可检查的授权声明，未自动导入")
    rights = text(permissions)
    hrefs = [
        value for key, value in license_node.attrib.items() if key.endswith("href")
    ]
    rights += (" " + " ".join(hrefs)) if hrefs else ""
    return rights


def acquire_pmc(
    material_id, pmcid, *, fetcher=None, start_parse=True, identifiers=None
):
    from apps.curation.workflows import enqueue

    if not re.fullmatch(r"PMC\d{1,12}", pmcid or ""):
        raise SourceBlocked("无效的 PMC 标识")
    url = "https://pmc.ncbi.nlm.nih.gov/api/oai/v1/mh/?" + urlencode(
        {
            "verb": "GetRecord",
            "identifier": "oai:pubmedcentral.nih.gov:" + pmcid[3:],
            "metadataPrefix": "pmc",
        }
    )
    content = (fetcher or fetch)(url, 25 * 1024 * 1024)
    if identifiers is None:
        identifiers = Material.objects.get(pk=material_id).identifiers
    rights = validate_pmc(content, pmcid, identifiers)
    # PMC's full-text OAI endpoint only serves reusable content; preserve exact per-article terms.
    digest = store_blob(content)
    with transaction.atomic():
        version, _ = MaterialVersion.objects.get_or_create(
            material_id=material_id,
            sha256=digest,
            defaults={
                "media_type": "application/jats+xml",
                "filename": pmcid + ".xml",
                "source_url": url,
                "rights": rights,
            },
        )
        job = (
            enqueue(
                "parse",
                {"version_id": str(version.pk), "parser_version": PARSER_VERSION},
            )
            if start_parse
            else None
        )
    return {
        "version_id": str(version.pk),
        "next_job": str(job.pk) if job else None,
        "rights": rights,
    }


def upload_pdf(
    content, title, kind, rights, material_id=None, *, filename="", start_parse=True
):
    from apps.curation.workflows import enqueue

    if not content.startswith(b"%PDF-"):
        raise SourceBlocked("文件不是有效的 PDF")
    digest = store_blob(content)
    with transaction.atomic():
        if material_id:
            material = Material.objects.get(pk=material_id)
        else:
            material, _ = Material.objects.get_or_create(
                source_key="upload:" + digest,
                defaults={
                    "title": title,
                    "kind": kind,
                    "metadata": {"acquisition": "user_upload"},
                },
            )
        version, _ = MaterialVersion.objects.get_or_create(
            material=material,
            sha256=digest,
            defaults={
                "media_type": "application/pdf",
                "filename": Path(filename.replace("\\", "/")).name[:255]
                or digest[:12] + ".pdf",
                "rights": rights,
            },
        )
        job = (
            enqueue(
                "parse",
                {"version_id": str(version.pk), "parser_version": PARSER_VERSION},
            )
            if start_parse
            else None
        )
    return material, version, job


def parse_material(version_id, parser_version, *, start_extract=True):
    from apps.curation.workflows import enqueue

    if parser_version != PARSER_VERSION:
        raise SourceBlocked("解析器版本已变化，请创建新版本的解析任务")
    version = MaterialVersion.objects.get(pk=version_id)
    if version.integrity_status != "valid":
        raise SourceBlocked("该资料版本的来源关联存在问题，已隔离，不能解析或提取证据")
    content = read_blob(version.sha256)
    blocks = []
    if version.media_type == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise SourceBlocked("PDF 已加密，请上传可合法读取的版本")
        if len(reader.pages) > 1500:
            raise SourceBlocked("PDF 页数超过单次解析上限（1500 页）")
        for index, page in enumerate(reader.pages):
            value = page.extract_text() or ""
            if value.strip():
                blocks.append((f"page:{index + 1}", value))
    elif version.media_type == "application/jats+xml":
        root = strip_namespaces(xml(content))
        article = root.find(".//article")
        if article is None:
            raise SourceBlocked("未找到 JATS article")
        # Stable ordinal and section context, without merging captions/references into claims.
        for section in ("front/article-meta/abstract", "body"):
            area = article.find(section)
            if area is not None:
                for index, paragraph in enumerate(area.iter("p")):
                    value = text(paragraph)
                    if value:
                        blocks.append((f"{section}/p:{index + 1}", value))
    else:
        raise SourceBlocked("此版本是元数据，不能按全文提取证据")
    if not blocks or sum(len(value) for _, value in blocks) < 80:
        raise SourceBlocked(
            "未提取到足够文本。扫描 PDF 需要 OCR，当前未启用；不能按空白文本生成证据。"
        )
    if sum(len(value) for _, value in blocks) > 12_000_000:
        raise SourceBlocked("文本超过单次解析上限")
    with transaction.atomic():
        if not Passage.objects.filter(
            version=version, parser_version=parser_version
        ).exists():
            Passage.objects.bulk_create(
                [
                    Passage(
                        version=version,
                        parser_version=parser_version,
                        ordinal=i,
                        locator=loc,
                        text=value,
                    )
                    for i, (loc, value) in enumerate(blocks)
                ],
                batch_size=100,
            )
        job = (
            enqueue(
                "extract",
                {
                    "version_id": str(version.pk),
                    "parser_version": parser_version,
                    "pipeline_version": "evidence-v1",
                },
            )
            if start_extract
            else None
        )
    return {
        "version_id": str(version.pk),
        "passages": len(blocks),
        "next_job": str(job.pk) if job else None,
    }


def quarantine_version(version_id, reason):
    """Retain suspect source bytes and audit notes, while preventing downstream publication."""
    from django.utils import timezone

    from apps.curation.models import Evidence, KnowledgeCandidate, WorkItem

    with transaction.atomic():
        version = MaterialVersion.objects.select_for_update().get(pk=version_id)
        if version.integrity_status == "quarantined":
            return version
        version.integrity_status = "quarantined"
        version.integrity_note = f"{timezone.now().isoformat()} — {reason}"
        version.save(update_fields=["integrity_status", "integrity_note"])
        candidates = Evidence.objects.filter(passage__version=version)
        candidates.filter(publication_status="published").update(
            publication_status="withdrawn"
        )
        KnowledgeCandidate.objects.filter(
            version=version, publication_status="published"
        ).update(publication_status="withdrawn")
        WorkItem.objects.filter(
            payload__version_id=str(version.pk),
            state__in=["queued", "waiting_configuration", "running"],
        ).update(
            state="blocked",
            message="原文版本已隔离：" + reason,
            lease_token=None,
            lease_until=None,
        )
        WorkItem.objects.filter(
            kind="review_ai",
            payload__evidence_id__in=[
                str(pk) for pk in candidates.values_list("pk", flat=True)
            ],
            state__in=["queued", "waiting_configuration", "running"],
        ).update(
            state="blocked",
            message="原文版本已隔离，禁止发布",
            lease_token=None,
            lease_until=None,
        )
    return version
