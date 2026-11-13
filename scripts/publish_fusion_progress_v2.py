"""Append the important target-identity and loading findings to study 039."""

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from apps.curation.research_history import read_record, register_record

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / 'var/experiments/fusion-independent-20261113-v1'
DOC = PROJECT / 'docs/research/039-fusion-validation-progress-v2.md'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def publish():
    old = read_record('fusion-independent-20261113-progress')
    collisions = json.loads((ROOT/'target-name-collisions-rxnorm.json').read_text())
    drug = json.loads((ROOT/'drug-target-identity-observation.json').read_text())
    cache = json.loads((ROOT/'prefetch-cache-diagnosis.json').read_text())
    counts = collisions['by_kind']
    coverage = collisions['rxnorm_coverage']
    if (counts['drug']['dictionary_entries'] != 501 or counts['drug']['colliding_names'] != 42
            or counts['disease']['dictionary_entries'] != 752 or counts['disease']['colliding_names'] != 80
            or coverage['duplicate_english_names'] != 38 or coverage['unique_exact_ingredient_name'] != 23
            or drug['local_ingredient']['id'] != '2103164' or drug['export_id_selected']
            or drug['target_written'] or cache['target_written'] or collisions['target_written']):
        raise ValueError('Frozen identity or cache observation differs')
    record = copy.deepcopy(old)
    record.pop('content_sha256', None)
    record.pop('fingerprint', None)
    record.update(id='fusion-independent-20261113-progress-v2',
                  title='039 · 五篇融合验证与目标身份碰撞（进行中）',
                  date=datetime.now(timezone.utc).isoformat(),
                  summary='已有 1 条诊断结构隔离预览；发现目标实体同名多 ID，3 篇新文献仍在处理。')
    record['task']['outputs'].append('目标药物名称碰撞与标准成分覆盖')
    record['methods'].append({
        'id': 'target-identity', 'label': '目标药物名称与标准成分', 'version': collisions['version'],
        'metrics': [
            {'label': '同名目标药物检索词', 'value': '42 个', 'unit': '',
             'note': '在 501 个条目的冻结快照中统计；同名不必然是同一实体。'},
            {'label': '重复英文名有唯一精确 RxNorm 成分', 'numerator': 23, 'denominator': 38,
             'note': '仅完整英文名的精确比较，不能仅据此自动合并目标 ID。'},
        ],
        'conclusion': '同一成分可能对应多个历史目标 ID，当前保留歧义和全部引用，不自动新建或选择。',
    })
    record['findings'] = [item for item in record['findings'] if '预热' not in item]
    record['findings'].extend([
        '洛拉替尼／劳拉替尼同英文名分别对应两个目标 ID，两个都存在旧知识引用。',
        '整模型预热遭遇缓存回收，已改为仅在本次试验中按权重分段读取；记录时仍未完成新文献抽取。',
    ])
    record['next_steps'] = [
        '等待 3 篇新文献的模型抽取及结构对照完成，逐条对照 Astra 和原文并按独立事实去重。',
        '验证目标身份多 ID 的消费和查重方式，确认后再决定是否用于自动映射。',
        '观察下一次夜间全文发现任务的实际锁等待和游标进度。',
    ]
    record['identity']['task_version'] = 'fusion-independent-progress-v2'
    record['identity']['method_versions'].append(sha(PROJECT/'scripts/paced_safetensors.py'))
    record['identity']['reference_versions'].extend([
        sha(ROOT/'target-name-collisions-rxnorm.json'), sha(ROOT/'drug-target-identity-observation.json'),
        sha(ROOT/'prefetch-cache-diagnosis.json'),
    ])
    additions = [('目标名称碰撞与 RxNorm 覆盖', ROOT/'target-name-collisions-rxnorm.json'),
                 ('洛拉替尼目标身份观察', ROOT/'drug-target-identity-observation.json'),
                 ('模型缓存回收观察', ROOT/'prefetch-cache-diagnosis.json'), ('补充说明', DOC)]
    record['provenance'].extend({'label': label, 'artifact': str(path), 'sha256': sha(path)}
                                for label, path in additions)
    return register_record(record)


if __name__ == '__main__':
    print(json.dumps(publish(), ensure_ascii=False))
