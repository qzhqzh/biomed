"""Append a truthful interim research record while the 9B replay is pending."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from apps.curation.research_history import register_record
from scripts.freeze_effective_knowledge_sample import materialize, queue_rows

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / 'var/experiments/fusion-independent-20261113-v1'
OPS = Path('/appdata/02_resource/apps/biomed/operations/knowledge-processing')
DOC = PROJECT / 'docs/research/039-fusion-validation-in-progress.md'


def read(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def publish():
    manifest, source = read('manifest.json'), read('source-only.json')
    reference, reference_check = read('astra-source-reference.json'), read('astra-source-reference-validation.json')
    old, diagnostic = read('existing-controls-comparison.json'), read('diagnostic-development-v2.json')
    continuity, semantic = read('diagnostic-v2-continuity.json'), read('astra-diagnostic-review.json')
    semantic_check, target = read('astra-diagnostic-review-validation.json'), read('target-diagnostic-memory.json')
    identity = read('target-diagnostic-identity-readonly.json')
    classification = read('new-batch/classification/selection.json')
    if (manifest['articles'] != 5 or manifest['downloaded_fulltexts'] != 3 or manifest['reused_existing_fulltexts'] != 2
            or reference_check['articles'] != 5 or reference_check['source_quotes_verified'] != 88
            or reference_check['review_sha256'] != sha(ROOT/'astra-source-reference.json')
            or reference['source_only_sha256'] != sha(ROOT/'source-only.json')
            or old['candidates'] != 65 or diagnostic['candidates'] != 65
            or diagnostic['per_article']['42834928']['inactive_diagnostic_previews'] != 1
            or diagnostic['per_article']['42828699']['inactive_diagnostic_previews'] != 0
            or not diagnostic['developed_after_reading_reference'] or not continuity['same_preview_except_method_version']
            or continuity['v2_sha256'] != sha(ROOT/'diagnostic-development-v2.json')
            or semantic_check['review_sha256'] != sha(ROOT/'astra-diagnostic-review.json')
            or semantic['core_source_fact_judgment'] != 'supported' or semantic['complete_preview_judgment'] != 'partial'
            or not target['serializer_valid'] or target['production_written'] or target['network'] != 'none'
            or not identity['transaction_read_only'] or identity['target_written'] or identity['patient_data_queried']
            or not identity['tumor_id_present_and_name_matches'] or classification['categories'] != {'solid': 3}):
        raise ValueError('Interim research results differ from the frozen evidence')
    expected = {'ordered_exact': 1, 'reversed_partners': 0, 'different_partner': 0,
                'wrong_cancer': 0, 'missing_partner': 0, 'wrong_category': 0}
    if target['synthetic_approved_active_match_counts'] != expected:
        raise ValueError('Isolated target matching result differs')
    row = next(r for r in diagnostic['records'] if r['preview']['status'] == 'inactive_structure_preview')
    if row['group_id'] != 'E-42834928-0014':
        raise ValueError('Unexpected diagnostic candidate')
    source_article = next(a for a in source['articles'] if a['pmid'] == row['pmid'])
    passages = {p['id']: p for p in source_article['passages']}
    chosen = {'256e708ed1e043a19346e65470990362', 'a6f6f1e613204557a1f2ccf02e5ce6aa',
              'bceb4a881c0148c9ab2a9cc4118945d3'}
    quotes = []
    for item in semantic['evidence']:
        if item['passage_id'] not in chosen or item['quote'].startswith('Prior reports'):
            continue
        passage = passages[item['passage_id']]
        start = passage['text'].find(item['quote'])
        if start < 0 or passage['locator'] != item['locator']:
            raise ValueError('Published source quote does not match frozen passage')
        quotes.append({'passage_id': item['passage_id'], 'locator': item['locator'], 'quote': item['quote'],
                       'fields': [], 'start': start, 'end': start + len(item['quote'])})
    selected = [entry for entry in queue_rows(OPS) if entry['group_id'] == row['group_id']]
    if len(selected) != 1:
        raise ValueError('Original biomed candidate identity is ambiguous')
    original = materialize(OPS, selected)[0]
    participants = set(original['record']['fact'].get('entity_ids', []))
    entities = [{'id': e['ref'], 'type': e['kind'], 'text': e['label'],
                 'normalization_status': '原文候选；目标 ID 与双伙伴见映射预览', 'standard_ids': []}
                for e in original['record']['entities'] if e['ref'] in participants]
    payload = row['preview']['target_payload']
    conclusion = payload['conclusion_inputs'][0]
    case = {'id': row['group_id'], 'stored_candidate_id': original['candidate_id'], 'pmid': row['pmid'],
            'title': source_article['title'],
            'statement': '黏液样隆突性皮肤纤维肉瘤病例：靶向 RNA 测序发现 COL1A1–PDGFB 融合，辅助确认诊断。',
            'status': '诊断结构隔离预览，未启用', 'study_scope': '单病例', 'source_role': '同版原文',
            'review_note': 'Astra 支持核心事实，完整预览仅部分支持。入库链接指向原始 biomed 候选；新诊断映射仅在研究中。',
            'entities': entities, 'quotes': quotes,
            'mapping': {'status': '目标隔离保存及合成匹配通过',
                        'blockers': ['黏液样、RNA 检测和病例研究范围尚未成为目标机器约束。',
                                     '本规则在该病例上开发，未接入常驻流程；未写 tumor_api。'],
                        'preview': {'证据类别': '诊断', '主癌种': '隆突性皮肤纤维肉瘤',
                                    '主癌种 ID（已只读核对）': conclusion['disease_scope_inputs'][0]['tumor_id'],
                                    '融合靶点': conclusion['trigger_inputs'][0], '药物': [], '药效': '未赋值',
                                    '证据等级': '未分级', '目标启用': False}}}
    artifacts = [('固定五篇与许可', ROOT/'manifest.json'), ('Astra 原文参考', ROOT/'astra-source-reference.json'),
                 ('已有候选对照', ROOT/'existing-controls-comparison.json'),
                 ('诊断映射开发 v2', ROOT/'diagnostic-development-v2.json'),
                 ('诊断原文复核', ROOT/'astra-diagnostic-review.json'),
                 ('目标只读 ID 核对', ROOT/'target-diagnostic-identity-readonly.json'),
                 ('目标隔离匹配', ROOT/'target-diagnostic-memory.json'), ('研究说明', DOC)]
    record = {'schema_version': 'research-history-v1', 'id': 'fusion-independent-20261113-progress',
              'title': '039 · 五篇融合验证与全文发现恢复（进行中）',
              'date': datetime.now(timezone.utc).isoformat(), 'stage': 'knowledge', 'status': 'unscored',
              'task_kind': 'knowledge_candidates', 'sample_count': 5, 'sample_unit': '篇文献',
              'summary': '已有候选中形成 1 条诊断隔离预览并完成目标验证；3 篇新全文的 9B 筛选仍待完成。',
              'task': {'question': '融合知识能否安全用于目标证据中心，哪些条件或来源会造成误收？',
                       'input': '5 篇固定全文：3 篇新增、2 篇复用；旧文献保留所有已有候选。',
                       'outputs': ['5 篇 Astra 原文参考', '旧候选对照', '目标隔离诊断匹配', '全文发现锁恢复'],
                       'not_measured': ['3 篇新全文的完整 9B／关系模型输出', '五篇完整端到端新增数',
                                        '新诊断规则在独立正例上的效果', '生产患者或临床报告适用性']},
              'dataset': {'label': '5 篇融合相关全文；目的性样本', 'role': 'independent_sources_then_development',
                          'document_count': 5, 'notes': ['3 篇按标题和摘要线索目的性选取，另 2 篇复用已有全文。',
                          '诊断规则在阅读该病例后开发，结果不构成盲测。', '旧候选对照是 65 条，不是全文召回。']},
              'reference': {'label': 'Astra 来源参考与目标隔离运行', 'status': 'model_reference_not_human_gold',
                            'note': '88 处原文引用已逐字核对；目标测试使用合成样本。'},
              'methods': [
                  {'id': 'source', 'label': '来源与 4B 文献分类', 'version': 'fusion-independent-freeze-v1',
                   'metrics': [{'label': '新增文献的实体瘤分类', 'numerator': 3, 'denominator': 3,
                                'note': 'Astra 参考均为人体实体瘤病例；目的性样本，不能推断全库准确率。'},
                               {'label': '来源参考逐字引用', 'value': '88 处', 'unit': '',
                                'note': '定位正确不单独证明语义正确。'}],
                   'conclusion': '5 篇来源已固定；后续逐步比较抽取和目标映射。'},
                  {'id': 'diagnostic', 'label': '诊断融合映射开发', 'version': 'biomed.source-fusion-diagnostic-preview.v2',
                   'metrics': [{'label': '旧适配器离线通过', 'numerator': 0, 'denominator': 65,
                                'note': '仅两篇既有文献的候选。'},
                               {'label': '诊断结构预览', 'numerator': 1, 'denominator': 65,
                                'note': '看过病例后开发；核心获 Astra 支持，完整预览部分支持。'},
                               {'label': '目标合成反例未命中', 'numerator': 5, 'denominator': 5,
                                'note': '反向伙伴、不同伙伴、错误癌种、缺失伙伴和错误类别。'}],
                   'conclusion': '1 条诊断结构已隔离保存并精确匹配；未进入目标生产。'}],
              'findings': ['诊断病例是有价值的知识，不能因不含药物而判为文献无用。',
                           '细胞系的 56 条旧候选没有被转换为人体临床融合诊断。',
                           '原文的黏液样、RNA 检测、病例性质需保留，目标不自动约束这些条件。',
                           'SQLite 实测为 BUSY；历史同类错误未保存错误码，不能断定全部同因。',
                           '9B 模型试验因磁盘压力暂停，正在限速预热现有文件，未完成项不计分。'],
              'next_steps': ['在资源允许时恢复 9B、实体和关系抽取，并与原文参考逐条对照。',
                             '核对 3 篇新文献的独立可用知识数和错误归属，再决定是否接入后台。',
                             '观察下次夜间发现任务的锁等待、游标与实际处理进度。'],
              'identity': {'task_version': 'fusion-independent-progress-v1',
                           'sample_versions': [sha(ROOT/'manifest.json'), sha(ROOT/'source-only.json')],
                           'method_versions': [diagnostic['code_sha256'], sha(PROJECT/'scripts/knowledge_worker.py')],
                           'reference_versions': [sha(ROOT/'astra-source-reference.json'), sha(ROOT/'astra-diagnostic-review.json'),
                                                  sha(ROOT/'target-diagnostic-memory.json')], 'completeness': 'frozen'},
              'provenance': [{'label': label, 'artifact': str(path), 'sha256': sha(path)} for label, path in artifacts],
              'knowledge_candidates': [case]}
    return register_record(record)


if __name__ == '__main__':
    print(json.dumps(publish(), ensure_ascii=False))
