"""Publication policy. Model output is untrusted until anchoring and schema checks pass."""

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string

from apps.curation.models import (
    Concept,
    Evidence,
    MaterialVersion,
    Passage,
    Review,
    WorkItem,
)
from apps.curation.network import SourceBlocked


class ModelNotConfigured(Exception):
    pass


def provider():
    if not settings.CURATION_MODEL_BACKEND:
        raise ModelNotConfigured(
            "资料与原文定位已保存；尚未配置知识抽取和 AI 审核模型。"
        )
    engine = import_string(settings.CURATION_MODEL_BACKEND)()
    description = engine.describe()
    required = (
        "provider",
        "model",
        "extraction_prompt_version",
        "review_prompt_version",
    )
    if not isinstance(description, dict) or any(
        not isinstance(description.get(k), str) or not description[k].strip()
        for k in required
    ):
        raise SourceBlocked("模型适配器缺少模型或提示词版本，已停止发布")
    return engine, {k: description[k] for k in required} | {
        "policy_version": settings.CURATION_POLICY_VERSION
    }


def resolve_entity(link):
    if not isinstance(link, dict):
        raise SourceBlocked("实体链接格式无效")
    namespace, identifier = link.get("namespace"), str(link.get("identifier", ""))
    if namespace in ("MONDO", "RXNORM"):
        entity = (
            Concept.objects.select_related("release")
            .filter(
                release__namespace=namespace,
                release__active=True,
                identifier=identifier,
            )
            .first()
        )
        if not entity:
            raise SourceBlocked(f"标准库中未找到实体 {namespace}:{identifier}")
        return {
            "namespace": namespace,
            "identifier": identifier,
            "label": entity.label,
            "release": str(entity.release_id),
            "version": entity.release.version,
        }
    if namespace == "NCBIGene" and identifier.isdecimal():
        from apps.annotations.knowledge import store

        with store.pin_snapshot():
            result = store.search_genes(q=identifier, limit=10)
            entity = next(
                (
                    item
                    for item in result["results"]
                    if str(item["gene_id"]) == identifier
                ),
                None,
            )
            if entity:
                return {
                    "namespace": namespace,
                    "identifier": identifier,
                    "label": entity["symbol"],
                    "release": store.data_version(),
                }
    raise SourceBlocked("实体标识未得到标准库确认；未知信息不能自动补全")


def extract_candidates(job):
    from apps.curation.workflows import enqueue

    if not MaterialVersion.objects.filter(
        pk=job.payload["version_id"], integrity_status="valid"
    ).exists():
        raise SourceBlocked("该资料版本已隔离，不能提取证据")
    if job.result.get("extraction_complete"):
        return job.result
    engine, provenance = provider()
    passages = list(
        Passage.objects.filter(
            version_id=job.payload["version_id"],
            parser_version=job.payload["parser_version"],
        )
    )
    if not passages:
        raise SourceBlocked("没有可引用的原文段落")
    blocks = [{"id": str(p.pk), "locator": p.locator, "text": p.text} for p in passages]
    proposals = engine.extract(blocks)
    if not isinstance(proposals, list) or len(proposals) > 200:
        raise SourceBlocked("模型返回了不符合约定的候选证据")
    by_id = {str(p.pk): p for p in passages}
    validated = []
    for item in proposals:
        if not isinstance(item, dict):
            raise SourceBlocked("候选证据结构无效")
        passage = by_id.get(str(item.get("passage_id")))
        start, end, quote = item.get("start"), item.get("end"), item.get("quote")
        if (
            not passage
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(passage.text)
            or passage.text[start:end] != quote
        ):
            raise SourceBlocked("候选证据不能精确定位到原文，未进入发布流程")
        assertion, context = item.get("assertion"), item.get("context")
        if (
            not isinstance(assertion, str)
            or not assertion.strip()
            or len(assertion) > 10000
            or not isinstance(context, dict)
        ):
            raise SourceBlocked("候选证据缺少陈述或适用语境")
        # These fields must remain null when unknown. They cannot be inferred from a missing field.
        if not all(
            key in context for key in ("population", "study_type", "limitations")
        ):
            raise SourceBlocked("候选证据未明确人群、研究类型和局限（未知应填 null）")
        links = item.get("entities", [])
        if not isinstance(links, list) or len(links) > 30:
            raise SourceBlocked("实体关联格式无效")
        validated.append(
            {
                "passage": passage,
                "start": start,
                "end": end,
                "quote": quote,
                "assertion": assertion.strip(),
                "context": context,
                "entity_links": [resolve_entity(link) for link in links],
            }
        )
    with transaction.atomic():
        checkpoint = WorkItem.objects.get(pk=job.pk).result
        if checkpoint.get("extraction_complete"):
            return checkpoint
        # Frozen execution provenance survives a restart and is also attached to each review.
        ids = []
        for ordinal, data in enumerate(validated):
            candidate, _ = Evidence.objects.get_or_create(
                extraction=job, ordinal=ordinal, defaults=data
            )
            ids.append(str(candidate.pk))
            enqueue(
                "review_ai",
                {"evidence_id": str(candidate.pk), "provenance": provenance},
            )
        result = {
            "extraction_complete": True,
            "provenance": provenance,
            "candidates": ids,
            "count": len(ids),
        }
        WorkItem.objects.filter(pk=job.pk).update(result=result)
    return result


def review_ai(job):
    candidate = Evidence.objects.select_related("passage").get(
        pk=job.payload["evidence_id"]
    )
    if candidate.passage.version.integrity_status != "valid":
        raise SourceBlocked("该证据来源版本已隔离，禁止 AI 审核发布")
    existing = Review.objects.filter(job=job).first()
    if existing:
        return {"evidence_id": str(candidate.pk), "decision": existing.decision}
    engine, provenance = provider()
    if provenance != job.payload["provenance"]:
        raise SourceBlocked("模型或审核规则版本已变化，请建立新的抽取记录后审核")
    if candidate.passage.text[candidate.start : candidate.end] != candidate.quote:
        raise SourceBlocked("证据原文定位校验失败，禁止发布")
    result = engine.review(
        {
            "assertion": candidate.assertion,
            "context": candidate.context,
            "quote": candidate.quote,
            "source_text": candidate.passage.text,
            "entities": candidate.entity_links,
        }
    )
    if (
        not isinstance(result, dict)
        or result.get("decision") not in ("pass", "fail")
        or not isinstance(result.get("reason"), str)
        or not result["reason"].strip()
    ):
        raise SourceBlocked("AI 审核未提供有效决定和理由")
    with transaction.atomic():
        if not MaterialVersion.objects.filter(
            pk=candidate.passage.version_id, integrity_status="valid"
        ).exists():
            raise SourceBlocked("原文版本已被隔离，禁止发布")
        recorded, created = Review.objects.get_or_create(
            job=job,
            defaults={
                "evidence": candidate,
                "kind": "ai",
                "decision": result["decision"],
                "reason": result["reason"],
                "provenance": provenance,
            },
        )
        if not created:
            return {"evidence_id": str(candidate.pk), "decision": recorded.decision}
        if result["decision"] == "fail":
            Evidence.objects.filter(
                pk=candidate.pk, publication_status="published"
            ).update(ai_status="fail", publication_status="withdrawn")
            Evidence.objects.filter(pk=candidate.pk).exclude(
                publication_status="published"
            ).update(ai_status="fail")
        else:
            Evidence.objects.filter(pk=candidate.pk).update(ai_status="pass")
        if result["decision"] == "pass":
            # A human withdrawal is never overwritten by retries or a concurrent AI completion.
            Evidence.objects.filter(
                pk=candidate.pk, publication_status="draft", human_status="unchecked"
            ).update(publication_status="published", published_at=timezone.now())
    return {"evidence_id": str(candidate.pk), "decision": result["decision"]}


def human_review(candidate_id, user, decision, reason):
    if not user.is_authenticated or not user.has_perm("curation.change_evidence"):
        raise PermissionError("需要有证据审核权限的账号")
    if decision not in ("confirm", "flag") or not reason.strip():
        raise SourceBlocked("请提供抽查决定和理由")
    with transaction.atomic():
        candidate = Evidence.objects.select_for_update().get(pk=candidate_id)
        if candidate.ai_status != "pass" or candidate.publication_status != "published":
            raise SourceBlocked(
                "只能抽查已通过 AI 审核且仍在发布中的证据；撤回后不能覆盖历史重新发布"
            )
        Review.objects.create(
            evidence=candidate,
            kind="human",
            decision=decision,
            reason=reason.strip(),
            actor=user,
            provenance={"policy_version": settings.CURATION_POLICY_VERSION},
        )
        candidate.human_status = "confirmed" if decision == "confirm" else "flagged"
        if decision == "flag":
            candidate.publication_status = "withdrawn"
        candidate.save(update_fields=["human_status", "publication_status"])
    return candidate
