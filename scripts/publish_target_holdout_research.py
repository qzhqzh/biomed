"""Publish the bounded holdout and its exploratory fusion finding."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from apps.curation.research_history import register_record

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / 'var/experiments/target-holdout-20261113-v1'
DOC = PROJECT / 'docs/research/038-target-yield-holdout-v2.md'


def read(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def publish():
    manifest, development = read('manifest.json'), read('fusion-development-v1.json')
    business, correction = read('astra-business-reference.json'), read('astra-business-reference-correction.json')
    baseline = read('astra-baseline-review-validation.json')
    review, validation = read('astra-fusion-review.json'), read('astra-fusion-review-validation.json')
    runtime, identities = read('target-fusion-memory.json'), read('target-fusion-identities-readonly.json')
    source = read('source-only.json')
    if (manifest['article_count'] != 20 or manifest['candidate_count'] != 58
            or development['baseline_offline_count'] != 0 or development['inactive_structure_preview_count'] != 1
            or not development['development_after_reference_review'] or development['production_connected']
            or business['counts']['in_scope_primary_human_tumor'] != 7 or correction['pmid'] != '42848093'
            or correction['source_quotes_verified'] != 4 or baseline['judgments'] != {'supported': 15, 'partial': 15}
            or validation['judgment'] != 'partial' or validation['source_fact_judgment'] != 'supported'
            or validation['source_quotes_verified'] != 12
            or validation['review_sha256'] != sha(ROOT / 'astra-fusion-review.json')
            or runtime['development_sha256'] != sha(ROOT / 'fusion-development-v1.json')
            or identities['development_sha256'] != sha(ROOT / 'fusion-development-v1.json')
            or runtime['production_written'] or runtime['production_connected'] or runtime['network'] != 'none'
            or not runtime['serializer']['serializer_valid'] or not runtime['inactive_management_search']
            or runtime['inactive_patient_matches'] != 0 or not identities['transaction_read_only']
            or identities['target_written'] or identities['patient_data_queried']
            or not all(identities[key] for key in ('drugs_present', 'tumors_present',
                                                   'drugs_names_match_snapshot', 'tumors_names_match_snapshot'))
            or runtime['synthetic_approved_active_match_counts'] != {'ordered_exact': 1, 'reversed_partners': 0,
                'different_partner': 0, 'wrong_cancer': 0, 'missing_partner': 0}):
        raise ValueError('Frozen results do not match the intended research report')
    candidate = next(row for row in development['records']
                     if row['preview']['status'] == 'inactive_structure_preview')
    preview = candidate['preview']
    if (candidate['group_id'] != review['group_id'] or preview['submit_ready']
            or preview['automatic_clinical_matching'] or preview['target_written']):
        raise ValueError('Unexpected fusion candidate or activation state')
    payload = preview['target_payload']
    conclusion = payload['conclusion_inputs'][0]
    if (payload['is_active'] or payload['evidence_level'] != '-' or conclusion['potency'] != '-'
            or conclusion['trigger_inputs'][0]['target_key'] != 'EML4::ALK'):
        raise ValueError('Unexpected target representation')
    article = next(row for row in source['articles'] if str(row['pmid']) == review['pmid'])
    passages = {row['id']: row for row in article['passages']}
    quote_ids = ['ba407486a1614f92aa37b5902f401c69', '912e5625b3644a61a805a713cb162746',
                 '032484bc32d0425aa4b215e269dd9bd1', 'ea6e15cb36674cd48750931cdb71f22c']
    quotes = []
    for quote in review['evidence']:
        if quote['passage_id'] not in quote_ids:
            continue
        passage = passages[quote['passage_id']]
        start = passage['text'].find(quote['quote'])
        if start < 0 or passage['locator'] != quote['locator']:
            raise ValueError('Published quotation differs from frozen source')
        quotes.append({'passage_id': quote['passage_id'], 'locator': quote['locator'],
                       'quote': quote['quote'], 'fields': [], 'start': start, 'end': start + len(quote['quote'])})
    case = {'id': candidate['group_id'], 'pmid': candidate['pmid'], 'title': article['title'],
            'statement': '转移性黑色素瘤单病例：肿瘤组织 EML4–ALK 融合，阿来替尼治疗后报告约 9 个月疾病控制。',
            'status': '隔离结构预览，未启用', 'study_scope': '病例报告', 'source_role': '同版原文',
            'review_note': 'Astra 支持核心病例事实，完整预览为部分支持；材料差异、治疗时序和剂量需结合原文。',
            'entities': [
                {'id': 'disease', 'type': 'disease', 'text': 'metastatic melanoma',
                 'normalization_status': '当前目标主癌种 ID 已只读核验', 'standard_ids': []},
                {'id': 'fusion', 'type': 'biomarker', 'text': 'EML4–ALK fusion',
                 'normalization_status': '有序双基因，目标合成样本精确匹配', 'standard_ids': []},
                {'id': 'drug', 'type': 'drug', 'text': 'Alectinib',
                 'normalization_status': '当前目标药物 ID 已只读核验', 'standard_ids': []}],
            'quotes': quotes,
            'mapping': {'status': '目标隔离保存及合成匹配通过',
                        'blockers': ['部分来源条件没有目标机器约束，临床报告消费未验证。',
                                     '等级和药效未知；不将本预览用于自动临床匹配。'],
                        'preview': {'癌种 ID（当前已只读核验）': conclusion['disease_scope_inputs'][0]['tumor_id'],
                                    '药物 ID（当前已只读核验）': conclusion['drug_ids'][0],
                                    '融合靶点': conclusion['trigger_inputs'][0],
                                    '证据等级': '未分级', '药效': '未赋值', '目标启用': False,
                                    '精确融合匹配': '仅在内存库中模拟启用并使用合成样本'}}}
    files = [('固定样本清单', ROOT / 'manifest.json'), ('Astra 原文参考', ROOT / 'astra-business-reference.json'),
             ('综述病例参考更正', ROOT / 'astra-business-reference-correction.json'),
             ('旧候选原文复核', ROOT / 'astra-baseline-review-validation.json'),
             ('融合开发结果', ROOT / 'fusion-development-v1.json'),
             ('融合 Astra 复核', ROOT / 'astra-fusion-review.json'),
             ('目标只读身份核对', ROOT / 'target-fusion-identities-readonly.json'),
             ('目标隔离保存及匹配', ROOT / 'target-fusion-memory.json'), ('研究说明', DOC)]
    record = {'schema_version': 'research-history-v1', 'id': 'target-yield-holdout-20261113-v2',
              'title': '038 · 新文章知识利用率与融合隔离验收',
              'date': datetime.now(timezone.utc).isoformat(), 'stage': 'knowledge', 'status': 'evaluated',
              'task_kind': 'knowledge_candidates', 'sample_count': 20, 'sample_unit': '篇文献',
              'summary': '20 篇 58 条旧候选离线通过 0；1 条融合预览通过目标隔离保存和合成匹配，尚未用于生产报告。',
              'task': {'question': '新文章中的结构知识为何未通过目标适配？哪些修复有可核对依据？',
                       'input': '20 篇已完成全文、每篇最多 3 条既有候选。',
                       'outputs': ['Astra 原文参考及更正', '旧候选复核', '精确融合开发预览',
                                   '目标内存库保存及合成正反例'],
                       'not_measured': ['035 点突变修复在新正例上的效果', '本病例未分级治疗报告消费',
                                        '全文召回、全库比例与新模型吞吐', '真实患者适用性']},
              'dataset': {'label': '20 篇全文、58 条旧候选', 'role': 'bounded_holdout_and_followup_development',
                          'document_count': 20, 'notes': ['候选按既有事实选择，偏向治疗；每篇最多 3 条。',
                          '融合方法在阅读参考后开发，不能作为独立盲测。', '历史对照不是新样本。']},
              'reference': {'label': 'Astra 原文参考与逐字引用检查', 'status': 'model_reference_not_human_gold',
                            'note': '初审 7 篇，后续追加综述中的机构病例；原文支持不等于目标可用。'},
              'methods': [
                  {'id': 'reference', 'label': '固定文章与原文参考', 'version': 'target-holdout-v1',
                   'metrics': [{'label': '初审和补充后有人类分子事件及临床关系', 'numerator': 8,
                                'denominator': 20, 'note': '至少 8 篇；不等于治疗有效或目标通过。'},
                               {'label': '原文核对的旧候选核心内容获支持', 'numerator': 15,
                                'denominator': 30, 'note': '有目的选出的 30 条；另 15 条需补充，不是全库准确率。'}],
                   'conclusion': '有可提取线索，但参考样本不等于可用知识。'},
                  {'id': 'fusion', 'label': '旧适配与精确融合开发', 'version': development['version'],
                   'metrics': [{'label': '旧适配器离线通过', 'numerator': 0, 'denominator': 58,
                                'note': '仅这 58 条既有候选。'},
                               {'label': '开发得到的未启用融合结构预览', 'numerator': 1, 'denominator': 58,
                                'note': '看过参考后开发；Astra 核心支持、完整预览部分支持。'}],
                   'conclusion': '明确融合不能简单视为单基因泛融合；结果并非独立盲测。'},
                  {'id': 'target', 'label': '目标只读核对及隔离运行', 'version': runtime['version'],
                   'metrics': [{'label': '当前目标药物和主癌种 ID 与字典一致', 'numerator': 2, 'denominator': 2,
                                'note': '只读查询；未查询患者。'},
                               {'label': '目标内存库保存与正确融合匹配', 'numerator': 1, 'denominator': 1,
                                'note': '保存未启用；匹配仅内存库模拟启用和合成样本。'},
                               {'label': '反向融合、不同伙伴、错误癌种及缺失伙伴未命中',
                                'numerator': 4, 'denominator': 4, 'note': '合成反例。'}],
                   'conclusion': '目标能够精确表达该病例核心；目标生产报告与来源条件仍需单独处理。'}],
              'findings': ['本轮缺少 035 修复适用的已确认新点突变正例，不能评价其独立增益。',
                           'Bonsai 权重加载受磁盘等待影响，未进入推理；没有新模型速度或效果结果。',
                           'Astra 补充发现综述中的机构病例，不能只凭综述类型排除。',
                           '目标同精确融合、药物及癌种的已有键为 0；有限查询不代表全库语义去重。',
                           '目标未分级治疗报告限制和来源条件不能机器约束的问题仍在。'],
              'next_steps': ['定位新增全文发现的数据库错误，保持查询有界。',
                             '补充未参与开发的新融合正例和反例，比较独立通过数，再考虑后台接入。',
                             '补充 035 点突变修复适用的新正例。'],
              'identity': {'task_version': 'target-yield-holdout-v2',
                           'sample_versions': [sha(ROOT / 'manifest.json')],
                           'method_versions': [development['code_sha256'], sha(ROOT / 'target-fusion-memory.json')],
                           'reference_versions': [sha(ROOT / 'astra-business-reference.json'),
                                                  sha(ROOT / 'astra-business-reference-correction.json'),
                                                  sha(ROOT / 'astra-fusion-review.json'),
                                                  sha(ROOT / 'target-fusion-identities-readonly.json')], 'completeness': 'frozen'},
              'provenance': [{'label': label, 'artifact': str(path), 'sha256': sha(path)} for label, path in files],
              'knowledge_candidates': [case]}
    return register_record(record)


if __name__ == '__main__':
    print(json.dumps(publish(), ensure_ascii=False))
