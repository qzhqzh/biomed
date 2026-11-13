"""Bounded collection steps on the existing durable queue, with immutable PMID slices."""

import hashlib
import json
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from datetime import timezone as datetime_timezone
from urllib.parse import urlencode
from xml.etree.ElementTree import ParseError

from django.core.cache import cache
from django.db import OperationalError, transaction
from django.db.models import Count, Min, Q, Sum
from django.utils import timezone

from apps.curation.classification import (
    TAXONOMIES,
    classify,
    classify_metadata,
    taxonomy_hash,
    taxonomy_snapshot,
)
from apps.curation.collection_network import (
    CollectionHTTP,
    CollectionRetry,
    CollectionStopped,
    LeaseLost,
    check_lease,
)
from apps.curation.collection_recovery import (
    COMPLETED_REASON,
    MAX_FAILURES,
    RETRY_DELAYS,
    alert_pause,
    current_job,
    record_completion,
    record_retry,
    resolve_alerts,
    retry_delay,
)
from apps.curation.db import LowerIdentifier
from apps.curation.materials import (
    EUTILS,
    PARSER_VERSION,
    acquire_pmc,
    parse_material,
    prepare_pubmed_article,
    pubmed_metadata,
    pubmed_record_id,
    pubmed_records,
    save_prepared_pubmed_article,
    validate_pmc,
)
from apps.curation.models import (
    CollectionEntry,
    CollectionPartition,
    LiteratureClassification,
    LiteratureCollection,
    Material,
    MaterialVersion,
    WorkItem,
)
from apps.curation.network import SourceBlocked, xml
from apps.curation.storage import read_blob
from config.database import transient_database_error

DEFAULT_LIMITS = {
    "max_items": 50,
    "max_fulltexts": 5,
    "max_requests": 100,
    "max_bytes": 100 * 1024 * 1024,
    "interval_seconds": 2,
    "bytes_per_second": 256 * 1024,
    "page_size": 50,
}
LIMIT_RANGES = {
    "max_items": (1, 2000000),
    "max_fulltexts": (0, 2000),
    "max_requests": (1, 100000),
    "max_bytes": (1024 * 1024, 64 * 1024**3),
    "interval_seconds": (1, 60),
    "bytes_per_second": (16 * 1024, 1024 * 1024),
    "page_size": (1, 100),
}
ACTIVE = ("estimating", "running")
FULL_COLLECTION_LIMITS = {
    "max_items": 2000000,
    "max_requests": 60000,
    "max_bytes": 64 * 1024**3,
}
PUBMED_SLICE_LIMIT = 9999
MIN_PUBMED_UID = 1
MAX_PUBMED_UID = 9999999999
INITIAL_UID_PIVOT = 50000000


def validated_limits(overrides, original=None):
    if not isinstance(overrides, dict) or set(overrides) - set(DEFAULT_LIMITS):
        raise SourceBlocked("采集预算包含未知字段")
    values = (original or DEFAULT_LIMITS) | overrides
    for key, (low, high) in LIMIT_RANGES.items():
        value = values[key]
        if type(value) is not int or not low <= value <= high:
            raise SourceBlocked(f"{key} 必须是 {low} 至 {high} 之间的整数")
    return values


def schedule(run, delay=0):
    from apps.curation.workflows import enqueue

    job = enqueue(
        "collect_literature", {"collection_id": str(run.pk), "step": run.step}
    )
    if delay or run.retry_not_before:
        WorkItem.objects.filter(pk=job.pk, state="queued").update(
            available_at=max(
                job.available_at,
                timezone.now() + timedelta(seconds=delay),
                run.retry_not_before or timezone.now(),
            )
        )
    return job


def create_collection(start_date, end_date, limits=None):
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end or start.year < 1800 or end > timezone.now().date():
        raise SourceBlocked("日期范围无效；可指定历史日期回填，结束日期不能晚于今天")
    limits = validated_limits(limits or {})
    taxonomy = taxonomy_snapshot()
    with transaction.atomic():
        if LiteratureCollection.objects.filter(state__in=ACTIVE).exists():
            raise SourceBlocked("已有批次正在估算或采集，请先暂停或完成该批次")
        run = LiteratureCollection.objects.create(
            start_date=start,
            end_date=end,
            query=taxonomy["query"],
            taxonomy=taxonomy,
            limits=limits,
        )
        CollectionPartition.objects.create(
            collection=run, start_date=start, end_date=end
        )
        schedule(run)
    return run


def control_collection(pk, action, limits=None):
    with transaction.atomic():
        run = LiteratureCollection.objects.select_for_update().get(pk=pk)
        if action == "pause":
            if run.state in (*ACTIVE, "paused"):
                run.state = "paused"
                run.message = "用户暂停；正在传输的单次响应完成后停止，不再请求下一篇"
                run.save()
            return run
        if action != "resume":
            raise SourceBlocked("操作必须是 pause 或 resume")
        job = current_job(run)
        if run.state in ACTIVE and job and job.state in ("queued", "running"):
            raise SourceBlocked("该批次已在运行或等待退避，无需重复启动")
        if run.state == "complete":
            raise SourceBlocked("此检索快照已完成；更新日期范围请创建新批次")
        if (
            LiteratureCollection.objects.filter(state__in=ACTIVE)
            .exclude(pk=run.pk)
            .exists()
        ):
            raise SourceBlocked("另一个批次正在运行，请先暂停")
        updated = validated_limits(limits or {}, run.limits)
        entry_counts = run.entries.aggregate(
            total=Count("id"),
            saved=Count("metadata_version_id"),
            attempted=Count(
                "id", filter=Q(fulltext_state__in=("parsed", "reused", "unavailable"))
            ),
            non_pending=Count("id", filter=~Q(fulltext_state="pending")),
        )
        if updated["max_items"] < entry_counts["total"]:
            raise SourceBlocked(
                "累计文献上限不能低于已发现的 PMID 数量；待下载条目也占用名额"
            )
        if updated["max_fulltexts"] < entry_counts["attempted"]:
            raise SourceBlocked("累计全文上限不能低于已经处理的全文尝试数")
        if job and job.state == "running" and updated != run.limits:
            raise SourceBlocked("当前步骤尚未完成，请等待断点保存后再调整预算")
        metadata_pending = entry_counts["saved"] < entry_counts["total"]
        if (
            not metadata_pending
            and entry_counts["total"] >= updated["max_items"]
            and entry_counts["non_pending"] >= updated["max_fulltexts"]
        ):
            raise SourceBlocked("已达到文献和全文上限，请提高本批上限后续跑")
        used = run.fetches.aggregate(n=Sum("charged_bytes"))["n"] or 0
        if (
            used >= updated["max_bytes"]
            or run.fetches.count() >= updated["max_requests"]
        ):
            raise SourceBlocked("请求或流量预算已用尽，请提高累计预算后续跑")
        if (
            updated["max_fulltexts"] > run.limits["max_fulltexts"]
            and run.phase == "metadata"
            and not metadata_pending
            and not run.partitions.filter(state__in=("pending", "captured")).exists()
        ):
            # A larger fulltext budget must reuse the finished metadata snapshot.
            # Keep its baseline completion time and incremental watermark intact.
            run.phase = "fulltext"
        run.limits = updated
        run.state = "estimating" if run.phase == "estimate" else "running"
        run.started_at = run.started_at or timezone.now()
        run.message = ""
        run.consecutive_errors = 0
        # Reuse a queued/running step: pause/resume cannot create a second in-flight request.
        if not job or job.state not in ("queued", "running"):
            run.step += 1
        run.save()
        resolve_alerts(run)
        schedule(run)
        return run


def start_full_collection(pk, observation, limits=None):
    """Persist a scope/time anchor, then resume. A count is never a completion mark."""
    with transaction.atomic():
        run = LiteratureCollection.objects.select_for_update().get(pk=pk)
        if (
            run.state in ACTIVE
            or WorkItem.objects.filter(
                kind="collect_literature",
                payload__collection_id=str(run.pk),
                state="running",
            ).exists()
        ):
            raise SourceBlocked("请先暂停采集并等待当前步骤保存，再设置全量范围")
        if not isinstance(observation, dict) or any(
            observation.get(key) != value
            for key, value in {
                "query": run.query,
                "start_date": run.start_date.isoformat(),
                "end_date": run.end_date.isoformat(),
            }.items()
        ):
            raise SourceBlocked("计数快照与本批检索范围不一致")
        count = observation.get("candidate_total")
        if type(count) is not int or count <= 0:
            raise SourceBlocked("计数快照缺少有效候选总数")
        observed = datetime.fromisoformat(
            observation["observed_at"].replace("Z", "+00:00")
        )
        now = timezone.now()
        if timezone.is_naive(observed) or observed > now:
            raise SourceBlocked("计数快照需要有效的带时区观测时间")
        first = datetime.fromisoformat(
            run.estimate["observed_at"].replace("Z", "+00:00")
        )
        if timezone.is_naive(first):
            raise SourceBlocked("原批次缺少带时区的起始观测时间")
        scope = {
            "query": run.query,
            "start_date": run.start_date.isoformat(),
            "end_date": run.end_date.isoformat(),
            "date_field": "pdat",
        }
        scope_hash = hashlib.sha256(
            json.dumps(scope, sort_keys=True).encode()
        ).hexdigest()
        existing = run.estimate.get("acquisition")
        if existing and existing.get("scope_sha256") != scope_hash:
            raise SourceBlocked("已保存的全量范围不能被改写；请创建新批次")
        acquisition = existing or {
            "version": "pubmed-full-scope-v1",
            "mode": "all_candidates",
            "recorded_at": now.isoformat(),
            "recorded_at_local": now.astimezone(
                datetime_timezone(timedelta(hours=8))
            ).isoformat(),
            "display_timezone": "Asia/Shanghai",
            "scope": scope,
            "scope_sha256": scope_hash,
            "reference_count": count,
            "reference_count_observed_at": observed.isoformat(),
            "reference_known_overlap": observation.get("known_pmids_matching_query"),
            "reference_remaining": observation.get("remaining_unique_candidates"),
            "baseline_started_at": min(first, observed).isoformat(),
            "baseline_completed_at": None,
            "incremental_watermark": None,
            "incremental_replay_from": (
                min(first, observed) - timedelta(days=7)
            ).isoformat(),
            "incremental_overlap_days": 7,
            "incremental_new_record_field": "crdt",
            "incremental_revision_field": "lr",
            "incremental_refetch_existing": True,
            "incremental_schedule_enabled": False,
            "periodic_reconciliation_required": True,
        }
        run.estimate = run.estimate | {"acquisition": acquisition}
        run.save(update_fields=["estimate", "updated_at"])
        return control_collection(pk, "resume", FULL_COLLECTION_LIMITS | (limits or {}))


@contextmanager
def checkpoint(item, started, delay=0, failed=False):
    with transaction.atomic():
        check_lease(item)
        run = LiteratureCollection.objects.select_for_update().get(
            pk=item.payload["collection_id"]
        )
        if run.step != item.payload["step"]:
            raise LeaseLost()
        was_active = run.state in ACTIVE
        yield run
        if not failed:
            run.consecutive_errors = 0
            run.retry_not_before = None
            if run.state in ACTIVE:
                run.message = ""
        acquisition = run.estimate.get("acquisition", {})
        completed = not failed and (
            run.state == "complete"
            or (
                run.state == "paused"
                and acquisition.get("mode") == "all_candidates"
                and acquisition.get("baseline_completed_at")
            )
        )
        if was_active and completed:
            record_completion(run)
        elif was_active and run.state == "paused":
            alert_pause(
                run,
                "retry_exhausted"
                if run.consecutive_errors >= MAX_FAILURES
                else "collection_paused",
            )
        run.step += 1
        run.active_seconds += time.monotonic() - started
        run.save()
        if run.state in ACTIVE:
            schedule(run, delay)


def partition_uid_filter(partition):
    # MAX_PUBMED_UID is the application's ten-digit ID domain, not a supported
    # Entrez range endpoint. Keep the tail open with NOT instead of a huge upper
    # bound that PubMed can silently collapse into a single-UID query.
    if partition.uid_end == MAX_PUBMED_UID:
        if partition.uid_start == MIN_PUBMED_UID:
            return "", False
        return f"1:{partition.uid_start - 1}[UID]", True
    return f"{partition.uid_start}:{partition.uid_end}[UID]", False


def validate_uid_translation(result, partition):
    clause, negated = partition_uid_filter(partition)
    if not clause:
        return
    translated = result.get("querytranslation")
    if not isinstance(translated, str):
        raise SourceBlocked("PubMed 未返回 UID 分片的检索翻译，保留断点待核对")
    translated = re.sub(r"[\s()]", "", translated).lower()
    ranges = list(re.finditer(r"(?<![\d:])[1-9][0-9]*(?::[1-9][0-9]*)?\[uid\]", translated))
    expected = {clause.lower()}
    if not negated and partition.uid_start == partition.uid_end:
        expected.add(f"{partition.uid_start}[uid]")
    if (len(ranges) != 1 or ranges[0].group() not in expected
        or not translated[:ranges[0].start()].endswith("not" if negated else "and")):
        raise SourceBlocked("PubMed 检索翻译未保留完整 UID 范围或排除条件，保留断点，未将空结果当作完成")


def search(http, run, partition, retmax=0):
    term = run.query
    clause, negated = partition_uid_filter(partition)
    if clause:
        term = f"({term}) {'NOT' if negated else 'AND'} ({clause})"
    url = (
        EUTILS
        + "esearch.fcgi?"
        + urlencode(
            {
                "db": "pubmed",
                "term": term,
                "retmode": "json",
                "retmax": retmax,
                "sort": "pub date",
                "datetype": "pdat",
                "mindate": partition.start_date.strftime("%Y/%m/%d"),
                "maxdate": partition.end_date.strftime("%Y/%m/%d"),
                "tool": "biomed",
            }
        )
    )
    data = json.loads(http.fetch(url, 1024 * 1024))
    if not isinstance(data, dict):
        raise SourceBlocked("PubMed 未返回有效计数，检索未推进")
    result = data.get("esearchresult", {})
    if not isinstance(result, dict) or result.get("errorlist"):
        raise SourceBlocked("PubMed 未返回有效计数，检索未推进")
    errors = [
        node[key]
        for node in (data, result)
        for key in ("error", "ERROR")
        if node.get(key)
    ]
    if errors:
        # ESearch can return HTTP 200 with an embedded backend 500 instead of a
        # count. Retry only this identifiable service failure, never invalid
        # queries, unknown errors or a silently missing count.
        if all(
            isinstance(error, str)
            and re.search(r"\bSearch Backend failed\b", error, re.IGNORECASE)
            and re.search(r"\bStatus:\s*(?:500|502|503|504)\b", error, re.IGNORECASE)
            for error in errors
        ):
            raise CollectionRetry("PubMed 检索服务暂时不可用（响应内服务错误），保留断点等待重试")
        raise SourceBlocked("PubMed 返回检索错误，检索未推进")
    if not str(result.get("count", "")).isdigit():
        raise SourceBlocked("PubMed 未返回有效计数，检索未推进")
    validate_uid_translation(result, partition)
    return result


def validate_partition_pmids(ids, count, partition):
    if (
        not isinstance(ids, list)
        or len(ids) != count
        or not all(
            isinstance(pmid, str)
            and re.fullmatch(r"[1-9][0-9]{0,9}", pmid)
            and partition.uid_start <= int(pmid) <= partition.uid_end
            for pmid in ids
        )
        or len(set(ids)) != count
    ):
        raise SourceBlocked("检索结果不完整，或有无效、重复、超出分片范围的 PMID，未推进分片")


def cached_metadata(pmid):
    return (
        MaterialVersion.objects.filter(
            material__source_key="pubmed:" + pmid,
            media_type="application/pubmed+xml",
            integrity_status="valid",
        )
        .select_related("material")
        .order_by("-acquired_at")
        .first()
    )


def identifier_key(value):
    # SQLite's existing iexact comparison folds ASCII case only.
    return str(value).translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def identifier_owners(metadata_rows):
    """Look up this page's DOI/PMCID collisions using indexes under its write lock."""
    wanted = {
        key: {
            identifier_key(row["identifiers"][key])
            for row in metadata_rows
            if row["identifiers"].get(key)
        }
        for key in ("doi", "pmc")
    }
    if not any(wanted.values()):
        return {}
    owners = {}
    matches = Material.objects.annotate(
        doi_lower=LowerIdentifier("doi"),
        pmc_lower=LowerIdentifier("pmc"),
    ).filter(Q(doi_lower__in=wanted["doi"]) | Q(pmc_lower__in=wanted["pmc"]))
    for pk, identifiers in matches.values_list("pk", "identifiers"):
        for key, values in wanted.items():
            if identifiers.get(key):
                value = identifier_key(identifiers[key])
                if value in values:
                    owners.setdefault((key, value), set()).add(pk)
    return owners


def replace_identifier_owner(owners, material):
    # A concurrently created material may have changed before this transaction.
    # Reflect save_pubmed_article's current identifiers, including removed ones.
    for ids in owners.values():
        ids.discard(material.pk)
    for key in ("doi", "pmc"):
        if material.identifiers.get(key):
            owners.setdefault(
                (key, identifier_key(material.identifiers[key])), set()
            ).add(material.pk)


def prepare_classifications(metadata, taxonomy):
    revisions = [taxonomy]
    current = taxonomy_snapshot()
    if taxonomy_hash(current) != taxonomy_hash(taxonomy):
        revisions.append(current)
    return [(revision, classify_metadata(metadata, revision)) for revision in revisions]


def attach_metadata(
    entry, version, metadata, taxonomy, owners, reused=False, prepared=None
):
    for revision, result in prepared or prepare_classifications(metadata, taxonomy):
        classify(version, metadata, revision, prepared_result=result)
    entry.metadata_version = version
    entry.reused = reused
    # PMID is canonical. Equal DOI/PMCID on different PMIDs is a reviewable conflict, never a silent merge.
    identifiers = metadata["identifiers"]
    entry.identifiers = identifiers
    if any(
        owners.get((key, identifier_key(identifiers[key])), set())
        - {version.material_id}
        for key in ("doi", "pmc")
        if identifiers.get(key)
    ):
        entry.message = (
            "DOI/PMCID 与其他 PMID 冲突，保留记录待核对，未自动合并或获取全文"
        )
        entry.fulltext_state = "identity_conflict"
    entry.save()


def collect_metadata(run, http, item, started):
    pending = list(
        run.entries.filter(metadata_version__isnull=True)[: run.limits["page_size"]]
    )
    if pending:
        cached = [(entry, cached_metadata(entry.pmid)) for entry in pending]
        hits = [(entry, version) for entry, version in cached if version]
        if hits:
            parsed = []
            for entry, version in hits:
                records = pubmed_records(xml(read_blob(version.sha256)))
                if len(records) != 1 or pubmed_record_id(records[0]) != entry.pmid:
                    raise SourceBlocked("已有元数据快照的 PMID 不一致，请核对")
                metadata = pubmed_metadata(records[0])
                parsed.append(
                    (
                        entry,
                        version,
                        metadata,
                        prepare_classifications(metadata, run.taxonomy),
                    )
                )
            with checkpoint(item, started) as current:
                owners = identifier_owners([metadata for _, _, metadata, _ in parsed])
                for entry, version, metadata, prepared in parsed:
                    attach_metadata(
                        entry,
                        version,
                        metadata,
                        current.taxonomy,
                        owners,
                        reused=True,
                        prepared=prepared,
                    )
            return
        ids = [entry.pmid for entry in pending]
        url = (
            EUTILS
            + "efetch.fcgi?"
            + urlencode(
                {
                    "db": "pubmed",
                    "id": ",".join(ids),
                    "retmode": "xml",
                    "tool": "biomed",
                }
            )
        )
        content = http.fetch(url, 8 * 1024 * 1024)
        articles = pubmed_records(xml(content))
        by_id = {pubmed_record_id(article): article for article in articles}
        if set(by_id) != set(ids) or len(articles) != len(ids):
            raise SourceBlocked("PubMed 返回的 PMID 集合与本页不一致，保留断点待重试")
        prepared = {pmid: prepare_pubmed_article(by_id[pmid]) for pmid in ids}
        classifications = {
            pmid: prepare_classifications(row["metadata"], run.taxonomy)
            for pmid, row in prepared.items()
        }
        with checkpoint(item, started) as current:
            owners = identifier_owners([row["metadata"] for row in prepared.values()])
            for entry in pending:
                material, version, metadata = save_prepared_pubmed_article(
                    prepared[entry.pmid], url
                )
                replace_identifier_owner(owners, material)
                attach_metadata(
                    entry,
                    version,
                    metadata,
                    current.taxonomy,
                    owners,
                    prepared=classifications[entry.pmid],
                )
            current.consecutive_errors = 0
        return
    remaining = run.limits["max_items"] - run.entries.count()
    partition = (
        run.partitions.filter(state__in=("pending", "captured"))
        .order_by("-end_date", "-start_date", "-uid_end", "-uid_start")
        .first()
    )
    if remaining <= 0 or partition is None:
        with checkpoint(item, started) as current:
            current.phase = "fulltext"
            acquisition = current.estimate.get("acquisition")
            if (
                partition is None
                and acquisition
                and not acquisition.get("baseline_completed_at")
            ):
                # This is a completed partition scan, not a verified incremental watermark.
                acquisition["baseline_completed_at"] = timezone.now().isoformat()
                current.estimate = current.estimate | {"acquisition": acquisition}
        return
    if partition.state == "captured":
        ids = partition.pmids[
            partition.offset : partition.offset
            + min(remaining, run.limits["page_size"])
        ]
        validate_partition_pmids(ids, len(ids), partition)
        with checkpoint(item, started):
            for pmid in ids:
                CollectionEntry.objects.get_or_create(collection=run, pmid=pmid)
            partition.offset += len(ids)
            if partition.offset >= len(partition.pmids):
                partition.state = "done"
            partition.save()
        return
    # Count first, then capture the entire bounded slice. Large slices never download IDs
    # that would be discarded when splitting, and resumptions don't depend on History TTLs.
    retmax = PUBMED_SLICE_LIMIT if partition.count is not None else 0
    result = (
        {"count": str(partition.count)}
        if partition.count is not None and partition.count > PUBMED_SLICE_LIMIT
        else search(http, run, partition, retmax)
    )
    count = int(result["count"])
    with checkpoint(item, started) as current:
        if count > PUBMED_SLICE_LIMIT:
            if partition.start_date == partition.end_date:
                if partition.uid_start == partition.uid_end:
                    raise SourceBlocked("单日单 UID 分片仍超过 9999，来源计数异常；保留断点，未截断或跳过")
                mid = (
                    min(MAX_PUBMED_UID - 1, max(INITIAL_UID_PIVOT, partition.uid_start * 2))
                    if partition.uid_end == MAX_PUBMED_UID
                    else (partition.uid_start + partition.uid_end) // 2
                )
                children = [
                    (partition.start_date, partition.end_date, partition.uid_start, mid),
                    (partition.start_date, partition.end_date, mid + 1, partition.uid_end),
                ]
            else:
                mid = partition.start_date + (partition.end_date - partition.start_date) // 2
                children = [
                    (partition.start_date, mid, partition.uid_start, partition.uid_end),
                    (mid + timedelta(days=1), partition.end_date, partition.uid_start, partition.uid_end),
                ]
            for start, end, uid_start, uid_end in children:
                CollectionPartition.objects.get_or_create(
                    collection=run, start_date=start, end_date=end,
                    uid_start=uid_start, uid_end=uid_end,
                )
            partition.state = "split"
        elif retmax == 0 and count:
            partition.count = count
            partition.save()
            return
        else:
            ids = result.get("idlist", [])
            validate_partition_pmids(ids, count, partition)
            partition.pmids = ids
            partition.state = "captured" if ids else "done"
            partition.captured_at = timezone.now()
        partition.count = count
        partition.save()
        current.consecutive_errors = 0


def collect_fulltext(run, http, item, started):
    attempted = run.entries.filter(
        fulltext_state__in=("parsed", "reused", "unavailable")
    ).count()
    eligible = (
        run.entries.filter(
            fulltext_state="pending",
            identifiers__pmc__startswith="PMC",
        )
        .select_related("metadata_version__material")
        .first()
    )
    if attempted >= run.limits["max_fulltexts"] or eligible is None:
        with checkpoint(item, started) as current:
            exhausted = not current.partitions.filter(
                state__in=("pending", "captured")
            ).exists()
            complete = exhausted and eligible is None
            if current.state == "running":
                current.state = "complete" if complete else "paused"
                current.message = (
                    "检索快照已处理完成；全文按单独预算处理"
                    if complete
                    else "本批已停止，等待评估。提高累计文献 / 全文上限后可续跑"
                )
                if (
                    current.estimate.get("acquisition", {}).get("mode")
                    == "all_candidates"
                ):
                    current.message = (
                        "本范围候选元数据已扫描完成；全文按单独预算处理，增量补扫尚未执行"
                        if exhausted
                        else "达到运行保护上限，全量采集尚未完成；断点已保留"
                    )
            current.phase = "metadata"
        return
    material = eligible.metadata_version.material
    existing = None
    for candidate in material.versions.filter(
        media_type="application/jats+xml", integrity_status="valid"
    ).order_by("-acquired_at"):
        try:
            validate_pmc(
                read_blob(candidate.sha256),
                eligible.identifiers["pmc"],
                eligible.identifiers,
            )
            existing = candidate
            break
        except SourceBlocked:
            # A valid historical version can belong to an earlier metadata identity.
            # Keep it intact; it must not be attached to this collection entry.
            continue
    try:
        if existing:
            version_id = str(existing.pk)
        else:
            result = acquire_pmc(
                str(material.pk),
                eligible.identifiers["pmc"],
                fetcher=http.fetch,
                start_parse=False,
                identifiers=eligible.identifiers,
            )
            version_id = result["version_id"]
        check_lease(item)
        parsed = parse_material(version_id, PARSER_VERSION, start_extract=False)
        with checkpoint(item, started) as current:
            eligible.fulltext_version_id = version_id
            eligible.fulltext_state = "reused" if existing else "parsed"
            eligible.message = f"已解析 {parsed['passages']} 段；未启动模型抽取"
            eligible.save()
            current.consecutive_errors = 0
    except CollectionStopped:
        raise
    except SourceBlocked as exc:
        with checkpoint(item, started):
            eligible.fulltext_state = "unavailable"
            eligible.message = str(exc)
            eligible.save()


def collection_step(item):
    started = time.monotonic()
    run = LiteratureCollection.objects.get(pk=item.payload["collection_id"])
    if run.step != item.payload["step"] or run.state not in ACTIVE:
        return {"state": run.state, "message": "该步骤已处理或批次已暂停"}
    http = CollectionHTTP(item)
    try:
        if run.phase == "estimate":
            partition = run.partitions.get(
                start_date=run.start_date, end_date=run.end_date,
                uid_start=MIN_PUBMED_UID, uid_end=MAX_PUBMED_UID,
            )
            result = search(http, run, partition)
            with checkpoint(item, started) as current:
                partition.count = int(result["count"])
                partition.save(update_fields=["count"])
                current.estimate = current.estimate | {
                    "candidate_count": int(result["count"]),
                    "query_translation": result.get("querytranslation", ""),
                    "observed_at": timezone.now().isoformat(),
                }
                current.phase = "metadata"
                if current.state == "estimating":
                    current.state = "ready"
                current.message = "规模估算完成；尚未下载文献，启动后按本批预算采集"
        elif run.phase == "metadata":
            collect_metadata(run, http, item, started)
        else:
            collect_fulltext(run, http, item, started)
    except LeaseLost:
        return {"message": "租约或步骤已变化，未覆盖新进度"}
    except (CollectionRetry, ParseError, json.JSONDecodeError) as exc:
        scheduling_wait = getattr(exc, "scheduling_wait", False)
        delay = (
            exc.retry_after
            if scheduling_wait
            else retry_delay(run.consecutive_errors, getattr(exc, "retry_after", 0))
        )
        message = (
            str(exc) if isinstance(exc, CollectionRetry) else "来源响应不完整或格式无效"
        )
        with checkpoint(item, started, delay=delay, failed=True) as current:
            if scheduling_wait:
                # A source's permitted window is a schedule, not a failed request.
                # Preserve real prior failures until a request actually succeeds.
                current.retry_not_before = max(
                    current.retry_not_before or timezone.now(),
                    timezone.now() + timedelta(seconds=delay),
                )
                if current.state in ACTIVE:
                    current.message = f"{message}；断点已保存，将按允许时间自动继续"
            else:
                record_retry(current, message, delay)
    except (SourceBlocked, ValueError) as exc:
        with checkpoint(item, started, failed=True) as current:
            if current.state in ACTIVE:
                current.state = "paused"
                current.message = (
                    str(exc)
                    if isinstance(exc, SourceBlocked)
                    else "来源响应格式无效；保留断点，请检查后续跑"
                )
    except OperationalError as exc:
        if not transient_database_error(exc):
            raise
        delay = retry_delay(run.consecutive_errors)
        # If the database is still unavailable, let the fenced queue recover this
        # active step after its lease expires; do not mark it permanently paused.
        with checkpoint(item, started, delay=delay, failed=True) as current:
            record_retry(current, "数据库暂时不可用，整页断点已保留", delay)
    except Exception:
        with checkpoint(item, started, failed=True) as current:
            if current.state in ACTIVE:
                current.state = "paused"
                current.message = (
                    "采集步骤异常，已暂停并保留断点；请检查工作流错误后续跑"
                )
        raise
    return {"collection_id": str(run.pk), "processed_step": item.payload["step"]}


def classification_statistics(run, entries, saved, *, cached=False):
    """Read large classification JSON once, only when the user requests it."""
    key = f"curation:classification-statistics:v1:{run.pk}"
    result = cache.get(key) if cached else None
    if result is not None:
        return result
    rows = (
        LiteratureClassification.objects.filter(
            version_id__in=entries.filter(metadata_version__isnull=False)
            .order_by()
            .values("metadata_version_id")
        )
        .values(
            "taxonomy_version",
            "taxonomy_sha256",
            "result__scope",
            "result__publication_status",
        )
        .annotate(count=Count("id"), first_created_at=Min("created_at"))
        .order_by()
    )
    revisions, scopes, statuses = {}, {}, {}
    for row in rows:
        sha = row["taxonomy_sha256"]
        revision = revisions.setdefault(
            sha,
            {
                "taxonomy_version": row["taxonomy_version"],
                "taxonomy_sha256": sha,
                "count": 0,
                "first_created_at": row["first_created_at"],
            },
        )
        revision["count"] += row["count"]
        revision["first_created_at"] = min(
            revision["first_created_at"], row["first_created_at"]
        )
        scope = row["result__scope"]
        scopes.setdefault(sha, {})[scope] = (
            scopes.get(sha, {}).get(scope, 0) + row["count"]
        )
        status = row["result__publication_status"]
        if status is not None:
            statuses.setdefault(sha, {})[status] = (
                statuses.get(sha, {}).get(status, 0) + row["count"]
            )
    ordered = sorted(revisions.values(), key=lambda row: row["taxonomy_sha256"])
    ordered.sort(key=lambda row: row["first_created_at"], reverse=True)
    result = {
        "revisions": ordered,
        "scopes": scopes,
        "publication_statuses": statuses,
        "as_of": timezone.now().isoformat(),
        "saved": saved,
    }
    if cached:
        cache.set(key, result, 60)
    return result


def collection_entry_counts(run, *, cached=False):
    """Short UI snapshot; never used for budget enforcement or queue decisions."""
    key = f"curation:collection-entry-counts:v1:{run.pk}"
    result = cache.get(key) if cached else None
    if result is not None:
        return result
    entry_groups = list(
        run.entries.order_by()
        .values("fulltext_state")
        .annotate(
            total=Count("id"),
            saved=Count("metadata_version_id"),
            reused_count=Count("id", filter=Q(reused=True)),
        )
    )
    fulltexts = {}
    for row in entry_groups:
        fulltexts[row["fulltext_state"]] = (
            fulltexts.get(row["fulltext_state"], 0) + row["total"]
        )
    result = {
        "saved": sum(row["saved"] for row in entry_groups),
        "discovered": sum(row["total"] for row in entry_groups),
        "reused": sum(row["reused_count"] for row in entry_groups),
        "fulltexts": fulltexts,
        "as_of": timezone.now().isoformat(),
    }
    if cached:
        cache.set(key, result, 60)
    return result


def collection_fetch_metrics(run, *, cached=False):
    key = f"curation:collection-fetch-metrics:v1:{run.pk}"
    result = cache.get(key) if cached else None
    if result is not None:
        return result
    metrics = run.fetches.aggregate(
        requests=Count("id"),
        wire_bytes=Sum("wire_bytes"),
        decoded_bytes=Sum("decoded_bytes"),
        charged_bytes=Sum("charged_bytes"),
        request_seconds=Sum("elapsed_seconds"),
        errors=Count("id", filter=Q(state="failed")),
        reservations=Count("id", filter=Q(state="running")),
    )
    result = {
        "metrics": {k: v or 0 for k, v in metrics.items()},
        "as_of": timezone.now().isoformat(),
    }
    if cached:
        cache.set(key, result, 60)
    return result


def collection_data(
    run, detail=False, classification_sha=None, *, summary=False, cached=False
):
    entries = run.entries.all()
    # Automatic summaries may lag briefly. Manually requested classification
    # details need a current denominator when their separate statistics refresh.
    counts = collection_entry_counts(run, cached=cached and summary)
    fetches = collection_fetch_metrics(run, cached=cached)
    saved = counts["saved"]
    discovered = counts["discovered"]
    statistics = (
        classification_statistics(
            run, entries, saved, cached=cached and not classification_sha
        )
        if not summary
        else {
            "revisions": [],
            "scopes": {},
            "publication_statuses": {},
            "as_of": None,
            "saved": None,
        }
    )
    revisions = statistics["revisions"]
    statistics_saved = statistics["saved"]
    complete = [
        row
        for row in revisions
        if row["count"] == statistics_saved and statistics_saved
    ]
    selected = next(
        (row for row in complete if row["taxonomy_sha256"] == classification_sha),
        None,
    )
    if classification_sha and selected is None:
        raise SourceBlocked("此分类版本不存在或尚未覆盖本批全部已入库文献")
    current_sha = taxonomy_hash(taxonomy_snapshot())
    current = next(
        (row for row in complete if row["taxonomy_sha256"] == current_sha), None
    )
    rule_revisions = [row for row in complete if row["taxonomy_version"] in TAXONOMIES]
    # Experimental model runs never become the default merely by finishing last.
    selected = selected or current or (rule_revisions[0] if rule_revisions else None)
    selected_sha = (
        selected["taxonomy_sha256"] if selected else taxonomy_hash(run.taxonomy)
    )
    classifications = LiteratureClassification.objects.filter(
        taxonomy_sha256=selected_sha
    )
    job = current_job(run)
    result = {
        "id": str(run.pk),
        "state": run.state,
        "phase": run.phase,
        "step": run.step,
        "start_date": run.start_date,
        "end_date": run.end_date,
        "taxonomy_version": run.taxonomy["version"],
        "classification_taxonomy_version": (
            selected["taxonomy_version"] if selected else run.taxonomy["version"]
        )
        if not summary
        else None,
        "classification_taxonomy_sha256": selected_sha if not summary else None,
        "classification_revisions": revisions,
        "classification_statistics_loaded": not summary,
        "statistics_as_of": statistics["as_of"],
        "classification_statistics_saved": statistics_saved,
        "limits": run.limits,
        "estimate": run.estimate,
        "message": run.message,
        "recovery": {
            "consecutive_failures": run.consecutive_errors,
            "max_failures": MAX_FAILURES,
            "retry_delays_seconds": RETRY_DELAYS,
            "retry_not_before": run.retry_not_before,
            "waiting": bool(
                run.state in ACTIVE
                and job
                and job.state == "queued"
                and job.available_at > timezone.now()
                and run.consecutive_errors
            ),
            "next_attempt_at": job.available_at
            if job and job.state == "queued"
            else None,
            "open_alerts": run.alerts.filter(resolved_at__isnull=True)
            .exclude(reason=COMPLETED_REASON)
            .count(),
        },
        "created_at": run.created_at,
        "started_at": run.started_at,
        "updated_at": run.updated_at,
        "active_seconds": round(run.active_seconds, 3),
        "counts_as_of": counts["as_of"],
        "metrics_as_of": fetches["as_of"],
        "discovered": discovered,
        "saved": saved,
        "reused": counts["reused"],
        "fulltexts": counts["fulltexts"],
        "scopes": statistics["scopes"].get(selected_sha, {}),
        "publication_statuses": statistics["publication_statuses"].get(
            selected_sha, {}
        ),
        "metrics": fetches["metrics"],
        "next_job": {
            "id": str(job.pk),
            "state": job.state,
            "available_at": job.available_at,
        }
        if job
        else None,
    }
    if detail and not summary:
        result["query"] = run.query
        result["partitions"] = list(
            run.partitions.values(
                "start_date", "end_date", "uid_start", "uid_end", "state", "count", "offset", "captured_at"
            )
        )
        shown = list(entries.select_related("metadata_version__material")[:100])
        by_version = dict(
            classifications.filter(
                version_id__in=[e.metadata_version_id for e in shown]
            ).values_list("version_id", "result")
        )
        result["entries"] = [
            {
                "pmid": entry.pmid,
                "material_id": str(entry.metadata_version.material_id)
                if entry.metadata_version
                else None,
                "title": entry.metadata_version.material.title
                if entry.metadata_version
                else "",
                "reused": entry.reused,
                "fulltext_state": entry.fulltext_state,
                "message": entry.message,
                "classification": by_version.get(entry.metadata_version_id),
            }
            for entry in shown
        ]
        result["entries_truncated"] = discovered > 100
    return result
