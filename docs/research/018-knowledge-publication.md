# 018 · 候选核验、发布与知识检索

2026-10-30（北京时间）。使用原5篇15组，新增实际Astra独立核验，完成biomed的候选→核验→发布→检索闭环。没有重跑9B、扩大样本、微调、增加概括或修改tumor_api。

## 实际结果

**15组核验，11组通过并实际发布，4组未通过留在候选库。** 人工状态均为未抽查，AI通过即可发布，人工抽查独立。这不是继承旧12/15为发布许可，也不是新的9B准确率评测；审核口径不同，不能直接作模型性能升降比较。

| 未发布组 | 本轮发现 |
| --- | --- |
| K05 | 治疗仅列两个剂量队列，结果却合并三个队列，改变适用范围 |
| K12 | 细胞IC50/活性结果的scope同时写了in vivo，细胞与动物范围错绑；不要求收纳细胞系或实验剂量为目标实体 |
| K13 | 肺模型统计值关联到肺与脑模型，部分结果缺少肺限定 |
| K14 | 首次MRI在放疗后3周，第二次是其后6周，错写为放疗后3周和6周 |

原fact、旧assessment及441条原文位置不变。新审核绑定冻结输入SHA，独立保存理由和证据；失败不会自动改写候选。K15按论文引文综述语境通过，不声称病例实际给药或完成现行监管核验。K07未来联合研究提议、K11既往T-DM1/当前T-DXd保留角色说明，不是同期联合疗效。

## 胆道癌＋durvalumab＋CRP

[实际检索](http://192.168.124.2:50011/knowledge/search/?disease=%E8%83%86%E9%81%93%E7%99%8C&drug=durvalumab&biomarker=CRP)，[localhost备用](http://localhost:50011/knowledge/search/?disease=%E8%83%86%E9%81%93%E7%99%8C&drug=durvalumab&biomarker=CRP)。

命中K08、K09，均来自PMID 42870461、PMCID PMC13662409、DOI 10.1002/cncr.70644。正文104例BTC患者接受含durvalumab治疗，摘要明确八中心回顾性chemoimmunotherapy背景；不是durvalumab单药试验，观察性关联不等于药物因果效果。

| 条目 | 分组及结果 |
| --- | --- |
| K08 | CRP flare responder／responder／nonresponder：DCR、ORR、PFS、OS均未见显著组间差异，不能改成正向预测结论 |
| K09 | CRP normal／normalized／non-normalized：中位PFS为13.0、6.0、2.0个月，p<.001；与K08不同分组分别保存 |

K08原句：`The DCR and ORR did not differ significantly between flare responders, responders, and nonresponders.`

K09的CI保留正文值，末组0.9–9.3个月，不以摘要另一分组模型数值替换。页面提供完整原句、数值、组别和整段展开，不增加概括。原文`Iin total`拼写也保持不变。

摘要定义BTC、CRP全称后，审核批准同篇别名；“胆道癌”仅为项目搜索译名，不冒充官方中文或英文原文。CRP与CRP kinetics分别保存。K09旧entity_ids漏药，本轮据原fact及P2批准durvalumab参与，旧fact不改。

## 流程与边界

1. 冻结现有候选、同版本原文，不向Astra输入旧评分。独立核对人群、药物、条件、数值和范围。
2. 脚本核对引文字符、原文、版本及SHA；AI通过才发布。
3. 为审核批准的关系实体和来源支持别名建索引；同一知识须满足全部查询条件。
4. 人工可具名确认或报告问题；报告问题或来源隔离会撤回。重放AI审核不能恢复已撤回条目，原文和历史保留。

新增KnowledgeReview、KnowledgeSearchTerm两表及候选的独立状态，保留旧Evidence/Review。未知标准ID不阻断来源内知识，也不升级为全局标准词条。具体药与治疗方式分开：旧drug_names中的类别按正确类型关联，不能用ICI替代durvalumab。

检索为批准词条的规范化精确匹配，支持大小写归一。不是标题、全文模糊检索。实测NLR替代CRP或ICI替代具体药均为0；CRP kinetics仅命中K09。尚无全库自动审核、跨论文全面合并、向量检索或自然语言问答。

溯源链为知识→审核引用→Passage→MaterialVersion→Material。文章保留PMID/DOI/原文件SHA；段落保留解析器、locator、Unicode字符区间和英文quote。新审核不覆盖原抽取或旧参考。

## 验证与持久化

- 发布14项、原候选13项、限定迁移5项和页面路由测试通过，前端构建通过。
- 实际新增15个审核，发布11条，82个检索词。重放新增0条、复用15条；原15组内容和441条英文位置回读一致。
- Astra核对106份引用及15个输入SHA。交互审阅墙钟261秒，含工具、阅读、构造和验证，不是纯模型推理延迟或规模化吞吐。
- 浏览器检查覆盖例子、负例、原文、桌面/手机、导航及人工入口；未代用户提交人工确认。实际结果见browser-validation.json。
- [研究历程018](http://192.168.124.2:50011/management/research/?experiment=knowledge-release-20261030)。实验文件在`var/experiments/knowledge-release-20261030/`，实际为`/appdata/02_resource/apps/biomed/research/artifacts/knowledge-release-20261030/`。
- 定向备份为`/appdata/02_resource/apps/biomed/operations/knowledge-release-20261030/small-tables-before.json`，含原3张小表和schema，不是整库备份。0011约0.95秒完成，仅检查4个相关小表FK，原候选数据及无关schema不变。

脚本：prepare_knowledge_review.py冻结；review_knowledge_candidates命令先dry-run再导入；verify_knowledge_publication.py回读；publish_knowledge_release.py登记。日常模型未配置时仍不生成假审核。本次是版本明确的Astra核验试验。

下一步先核对11条是否满足实际使用，再对同组明确错绑生成新候选版本、重新核验。本轮不自动修订，不追加模型链或扩大批量。
