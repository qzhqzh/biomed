"""Read-only observation report for completed knowledge batches and pending work.

No models, target connections, or database writes. Exact fact deduplication is
not semantic knowledge adjudication; possible semantic duplicates stay visible.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_ROOT = Path('/appdata/02_resource/apps/biomed/operations/knowledge-processing')
VERSION = 'knowledge-observation-report-v1'
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def exact_fact_key(record, exported):
    """Remove run-local identifiers, preserving all clinical facts/conditions.

    No paraphrase, subgroup, result, or alias collapsing. Citation IDs do not
    change a fact; entity identities and participant roles do.
    """
    entity_names = {e['ref']: digest({'kind': e.get('kind'), 'label': e.get('label')})
                    for e in record.get('entities', [])}

    def canonical(value):
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if key == 'passage' and isinstance(child, str) and re.fullmatch(r'P\d+', child):
                    continue
                if key == 'evidence' and isinstance(child, list) and all(
                        isinstance(v, str) and re.fullmatch(r'P\d+', v) for v in child):
                    continue
                if key == 'entity_roles' and isinstance(child, dict):
                    # Repeated labels may have distinct roles. A dict keyed by
                    # normalized label would silently discard one participant.
                    result[key] = sorted([{'entity': entity_names.get(ref, ref), 'roles': roles}
                                          for ref, roles in child.items()], key=digest)
                else:
                    result[key] = canonical(child)
            return result
        if isinstance(value, list):
            return [canonical(v) for v in value]
        if isinstance(value, str):
            return entity_names.get(value, value)
        return value

    fact = canonical(record.get('fact', {}))
    if isinstance(fact.get('entity_ids'), list):
        fact['entity_ids'] = sorted(fact['entity_ids'])
    return digest({'pmid': str(record['article']['pmid']), 'fact': fact,
                   'category': exported['target_payload'].get('evidence_category'),
                   'conclusions': exported['target_payload'].get('conclusion_inputs')})


def target_group_key(record, exported):
    """Coarse review flag only: conditions/results can differ within this group."""
    payload = exported['target_payload']
    return digest([str(record['article']['pmid']), payload.get('evidence_category'),
                   payload.get('conclusion_inputs')])


def batch_path(root, batch_id):
    if not isinstance(batch_id, str) or not ID.fullmatch(batch_id):
        raise ValueError('Invalid batch ID')
    return root / 'batches' / batch_id


def verified_batch(root, row):
    folder = batch_path(root, row['id'])
    no_solid = folder / 'batch-result.json'
    if no_solid.exists() and read(no_solid).get('state') == 'no_solid_articles':
        return [], [], {}, read(no_solid).get('articles', [])
    final = folder / 'final'
    knowledge, exported = read(final / 'knowledge.json'), read(final / 'tumor-api-export.json')
    receipt, summary = read(final / 'import-receipt.json'), read(final / 'summary.json')
    result = receipt.get('result', {})
    if (receipt.get('bundle_sha256') != file_sha(final / 'knowledge.json')
            or result.get('dry_run') is not False
            or 'entity_registry' not in result
            or exported.get('source_bundle_sha256') != digest(knowledge)):
        raise ValueError('Unverified local delivery or export checksum')
    records = knowledge['records']
    expected = {r['group_id'] for r in records}
    candidates = {r['group_id']: r['id'] for r in result['candidates']}
    if (len(expected) != len(records) or set(candidates) != expected
            or {r['group_id'] for r in exported['records']} != expected):
        raise ValueError('Knowledge, export and receipt groups differ')
    return records, exported['records'], candidates, summary['articles']


def observation_directory(root, observation):
    value = Path(observation)
    if value.is_absolute():
        return value
    if not ID.fullmatch(str(value)):
        raise ValueError('Observation must be a simple ID or an absolute directory')
    return root / 'observations' / value


def build_report(root, observation, now=None):
    root = Path(root)
    observation = observation_directory(root, observation)
    baseline = read(observation / 'baseline.json')
    started = baseline['started_at']
    if type(started) not in (int, float) or not math.isfinite(started) or started <= 0:
        raise ValueError('Invalid observation start')
    baseline_ids = baseline['baseline_batch_ids']
    if not isinstance(baseline_ids, list) or any(not isinstance(x, str) for x in baseline_ids):
        raise ValueError('Invalid baseline batch IDs')
    now = time.time() if now is None else now
    with closing(sqlite3.connect((root / 'queue.sqlite3').resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        # One short read snapshot; file IO and deduplication happen after close.
        db.execute('BEGIN')
        batches = [dict(r) for r in db.execute('SELECT id,state,attempts,seconds,created,updated,error,summary,next_attempt FROM batches ORDER BY created,id')]
        papers = [dict(r) for r in db.execute('SELECT batch_id,pmid,state FROM papers WHERE batch_id IS NOT NULL')]
    selected = [r for r in batches if r['id'] not in baseline_ids and started <= r['created'] <= now]
    selected_ids = {r['id'] for r in selected}
    old = [r for r in batches if r['id'] in baseline_ids and r['state'] == 'completed']
    history_exact, history_groups = set(), set()
    warnings = []

    def learn(record, export):
        if export.get('offline_ready'):
            history_exact.add(exact_fact_key(record, export))
            history_groups.add(target_group_key(record, export))

    for row in old:
        try:
            records, exports, _, _ = verified_batch(root, row)
            by_id = {r['group_id']: r for r in records}
            for exp in exports:
                learn(by_id[exp['group_id']], exp)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append({'scope': 'historical_batch', 'id': row['id'], 'error': str(exc)})
    for path in baseline.get('baseline_exports', []):
        try:
            for exp in read(path)['records']:
                if exp.get('offline_ready'):
                    learn(exp['biomed_companion'], exp)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append({'scope': 'historical_export', 'id': str(path), 'error': str(exc)})
    exact_keys, target_keys = set(), set()
    examples = []
    counts = Counter(saved_records=0, structurally_eligible=0, offline_ready_records=0,
                     papers_entering_extraction=0, verified_completed_batches=0)
    completed = [r for r in selected if r['state'] == 'completed']
    by_batch = []
    for row in completed:
        try:
            records, exports, candidates, articles = verified_batch(root, row)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append({'scope': 'observation_batch', 'id': row['id'], 'error': str(exc)})
            continue
        by_id = {r['group_id']: r for r in records}
        ready = [r for r in exports if r.get('offline_ready')]
        eligible = sum(r.get('assessment', {}).get('eligibility', {}).get('status') == 'eligible' for r in records)
        entered = sum(bool(r.get('extraction')) for r in articles)
        counts.update(saved_records=len(records), structurally_eligible=eligible,
                      offline_ready_records=len(ready), papers_entering_extraction=entered,
                      verified_completed_batches=1)
        by_batch.append({'id': row['id'], 'papers': len(articles), 'saved_records': len(records),
                         'structurally_eligible': eligible, 'offline_ready_records': len(ready),
                         'seconds': row['seconds']})
        for exp in ready:
            record = by_id[exp['group_id']]
            exact, target = exact_fact_key(record, exp), target_group_key(record, exp)
            examples.append({'batch': row['id'], 'group_id': exp['group_id'],
                             'candidate_id': candidates[exp['group_id']], 'pmid': str(record['article']['pmid']),
                             'exact_fact_sha256': exact, 'target_group_sha256': target,
                             'exact_seen_in_history': exact in history_exact,
                             'target_group_seen_in_history': target in history_groups,
                             'exact_seen_earlier_this_observation': exact in exact_keys})
            exact_keys.add(exact)
            target_keys.add(target)
    pending = [{k: r[k] for k in ('id', 'state', 'attempts', 'seconds', 'error', 'next_attempt')}
               for r in selected if r['state'] not in {'completed', 'superseded'}]
    attempted_ids = {r['id'] for r in selected if r['state'] not in {'ready', 'superseded'}}
    claimed = [p for p in papers if p['batch_id'] in selected_ids]
    attempted = [p for p in claimed if p['batch_id'] in attempted_ids]
    counts.update(papers_claimed=len(claimed), papers_attempted=len(attempted),
                  distinct_pmids_attempted=len({p['pmid'] for p in attempted}),
                  completed_batches=len(completed), exact_ready_fact_candidates=len(exact_keys),
                  new_exact_ready_fact_candidates=len(exact_keys - history_exact),
                  exact_historical_duplicates=len(exact_keys & history_exact),
                  target_groups_pending_semantic_review=len(target_keys),
                  target_groups_seen_in_history=len(target_keys & history_groups))
    status = read(root / 'status.json') if (root / 'status.json').exists() else {}
    current = {'activity': status.get('activity'), 'paused': status.get('paused'),
               'reason': status.get('reason'), 'progress': status.get('progress'),
               'status_updated_at': status.get('updated_at'),
               'status_age_seconds': max(0, now - status['updated_at']) if status.get('updated_at') else None}
    return {'version': VERSION, 'observation_id': observation.name, 'started_at': started,
            'generated_at': now, 'generated_at_local': datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')).isoformat(),
            'baseline_sha256': file_sha(observation / 'baseline.json'), 'counts': dict(counts),
            'batch_states': dict(Counter(r['state'] for r in selected)), 'batches': by_batch,
            'unfinished_or_failed_batches': pending, 'current': current,
            'ready_candidates': examples, 'warnings': warnings,
            'completed_counts_complete': not any(w['scope'] == 'observation_batch' for w in warnings),
            'history_comparison_complete': not any(w['scope'].startswith('historical') for w in warnings),
            'quality_scope': '精确事实去重候选，未做全批语义核验。措辞不同的同义知识仍可能重复；不等于真正净新增独立知识。',
            'target_written': False, 'database_written': False}


def markdown(report):
    c = report['counts']
    progress_note = ('尚无已完成批次；进行中的批次还不能评估最终知识产出。'
                     if not c['completed_batches'] else
                     f"本轮已完成 {c['completed_batches']} 批；以下产出仅统计已完成且回执有效的批次。")
    age = report['current']['status_age_seconds']
    heartbeat = '未知' if age is None else f'{age:.0f}秒前' + ('（状态已过期）' if age > 120 else '')
    lines = [f"# 知识处理观察汇总 · {report['generated_at_local']}", '',
             progress_note, '',
             f"尝试 {c['papers_attempted']} 篇（{c['distinct_pmids_attempted']} 个不同PMID）；已完成批次中 {c['papers_entering_extraction']} 篇进入提取。",
             f"已核对回执的保存记录 {c['saved_records']} 条，结构合格 {c['structurally_eligible']} 条，目标离线兼容 {c['offline_ready_records']} 条。", '',
             f"精确事实去重候选 {c['exact_ready_fact_candidates']} 条；其中与历史完全相同 {c['exact_historical_duplicates']} 条，未发现完全相同历史事实 {c['new_exact_ready_fact_candidates']} 条。",
             f"同文章/目标关系分组 {c['target_groups_pending_semantic_review']} 组，其中 {c['target_groups_seen_in_history']} 组在历史出现，需核读是否只是换了措辞或确有不同条件。",
             report['quality_scope'], '',
             f"批次状态：{json.dumps(report['batch_states'], ensure_ascii=False)}。",
             f"当前服务：{report['current']['activity']}；{report['current']['reason'] or ''}",
             f"状态心跳：{heartbeat}。",
             f"当前阶段：{json.dumps(report['current']['progress'], ensure_ascii=False)}。"]
    if report['unfinished_or_failed_batches']:
        lines += ['', '未完成或失败批次（不将其当作0产出的成品）：']
        lines += [f"- {r['id']}：{r['state']}，已用 {r['seconds']:.1f} 秒；{r['error']}"
                  for r in report['unfinished_or_failed_batches']]
    if report['warnings']:
        lines += ['', '**统计或历史对照不完整，请检查以下产物：**']
        lines += [f"- {w['scope']} / {w['id']}：{w['error']}" for w in report['warnings']]
    if report['ready_candidates']:
        lines += ['', '目标兼容候选示例（最多5条）：']
        lines += [f"- PMID {r['pmid']} / {r['group_id']}：[查看biomed知识](http://192.168.124.2:50011/knowledge/candidates/?candidate={r['candidate_id']})"
                  for r in report['ready_candidates'][:5]]
    lines += ['', '只保存biomed；未写tumor_api。服务进度：http://192.168.124.2:50011/management/workflows/', '']
    return '\n'.join(lines)


def atomic_write(path, text):
    path = Path(path)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        temporary.write_text(text, encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--observation', required=True, help='Observation ID under root/observations, or absolute directory')
    args = parser.parse_args()
    directory = observation_directory(args.root, args.observation)
    result = build_report(args.root, args.observation)
    atomic_write(directory / 'report.json', json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    atomic_write(directory / 'report.md', markdown(result))
    print(json.dumps({'report': str(directory / 'report.json'), 'counts': result['counts'],
                      'warnings': len(result['warnings'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
