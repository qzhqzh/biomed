# 生信原始数据库文件管理

2026-10-28 建立。统一资源根目录为 `/appdata/02_resource/databases`，沿用已有的 `hg19`、`hg38` 分类。这里保存可重复下载、校验、分发的原始参考文件；不会自动替换 biomed 或其他生产流程的注释输入。模型仍使用 `/data/models`。

## 本批范围

| 资源 | 固定版本 | 当前范围 |
| --- | --- | --- |
| NCBI RefSeq GRCh37.p13 | `GCF_000001405.25-RS_2024_09` | GFF3、assembly report、注释报告、版本说明、官方 MD5 |
| NCBI RefSeq GRCh38.p14 | `GCF_000001405.40-RS_2025_08` | 同上 |
| NCBI genomic FASTA | 上述相同 assembly accession | 两套完整序列集已下载、校验并登记 |
| MANE | 1.5，GRCh38 | 复用已有 summary，不重复下载；关联 RefSeq RS_2025_08、Ensembl 116 |
| ClinVar | `20261019` 固定日期快照 | GRCh37/38 VCF、TBI、官方 VCF MD5、导出问题记录已登记；另保留通用说明快照 |
| RefSeq RNA GenBank | 303 条精确版本转录本 | 今天已有的 26 个批响应硬链接复用，属于选定记录，非完整 RefSeq 发布 |

完成状态以根目录 `manifest.json` 为准，`.part` 与 `.staging` 中未登记的下载不能当作已收录。`catalog.tsv` 是可直接用表格软件打开的派生目录。

2026-10-28 11:47 UTC（北京时间19:47）验收完成：49个资源文件，共2,457,078,818字节（2.288GiB，包括复用资料），全部SHA256及12个具有官方MD5的文件校验通过，无未完成下载。26个RNA批文件与原缓存共享inode；重复导入已有27个文件时新增为0，manifest保持完全一致。登记工具6项测试、Ruff与Python3.9语法检查通过。最终记录为 `/appdata/02_resource/databases/validation-20261028.json`；未上传115、未修改生产注释输入。

官方固定来源：[RefSeq GRCh37](https://ftp.ncbi.nlm.nih.gov/genomes/all/annotation_releases/9606/GCF_000001405.25-RS_2024_09/)、[RefSeq GRCh38](https://ftp.ncbi.nlm.nih.gov/genomes/all/annotation_releases/9606/GCF_000001405.40-RS_2025_08/)、[MANE 1.5](https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/release_1.5/)、[ClinVar GRCh37 归档](https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh37/archive_2.0/2026/)、[ClinVar GRCh38 归档](https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/archive_2.0/2026/)。ClinVar 选择可固定的日期快照，不将目录中的周日期擅自标为“月版”。

## 目录与版本语义

```text
/appdata/02_resource/databases/
├── README.md
├── manifest.json                  # 权威登记，使用相对文件路径
├── catalog.tsv                    # 人可读目录
├── external_inventory.json        # 旧生产输入原位盘点，不纳入公开分发包
├── hg19/refseq/GCF_000001405.25-RS_2024_09/
├── hg38/refseq/GCF_000001405.40-RS_2025_08/
├── hg38/mane/1.5/
├── hg19/clinvar/20261019/
├── hg38/clinvar/20261019/
├── rna/refseq/selected-records-20261028/
└── .staging/                      # 断点、下载回执、导入计划；不是已完成目录
```

`hg19/hg38` 只是目录归类。真实身份是 **来源 + 数据集 + assembly accession.version + annotation release + SHA256**。例如 `GCF_000001405.40_GRCh38.p14_genomic.gff.gz` 的文件名有组装版本，却没有注释发布版本，必须依靠版本目录与 manifest 区分。

`rna/` 按功能归类为 RNA／转录本数据库；`refseq/` 标明来源。当前 `selected-records-20261028` 仅包含为外显子核验选取的303条精确版本RNA记录，不代表完整RefSeq RNA库。2026-10-28按用户要求从 `common/refseq-rna/` 调整至此，文件内容与原缓存硬链接不变，登记路径和导入计划同步更新。

每条登记保留原文件名、来源 URL、assembly 与 annotation release、字节数、SHA256、可用时的上游 MD5、准确获取时间或 null、登记时间、原路径，以及上游 Last-Modified 等证据。文件 mtime 不冒充下载时间或版本。MANE 旧缓存缺少准确获取时间，保存既有导入时间并明确其含义；RNA 旧缓存中的 1 个批次也保留获取时间未知。

同目标路径、同内容与版本复用已有登记；不同内容或来源/版本冲突停止，不覆盖旧文件。上游若修订相同版本同名文件，另建带日期/摘要的修订目录，再登记；不能覆盖已用于分析的原始输入。相同文件优先硬链接以减少物理占用；共享 inode 的原始文件不得原位修改，更新必须新建文件。跨文件系统复制完成后校验，原输入不删除。

## 参考 FASTA 的选择边界

本批是 NCBI 的 **full assembly**，保留 NCBI accession.version 序列名、补丁及替代序列。GRCh37.p13 共297条、GRCh38.p14 共709条，角色由配套 `assembly_report.txt` 明确。它们不是 UCSC `hg19.fa/hg38.fa`、`b37`、`hs37d5` 或 `no_alt` 的任意替代品。接入客户流水线前需要匹配序列集合、染色体命名、坐标与索引；本次只归档压缩原始文件，不生成多份解压副本或 BWA 索引。

GFF 是基因组映射注释，不自动等于转录本的可信生物学外显子数；原有外显子核验与版本结构见 [标准资源说明](standard-resources.md#transcript-structures)。

## ClinVar error.txt 的用途

`clinvar_20261019.vcf.error.txt` 是NCBI官方随VCF提供的导出问题记录，不是下载失败日志，也不能统一当成剔除名单。本次流式核对两套原始VCF：GRCh37的12组 `duplicate_location` 涉及25个Variation ID，按ID（含分号分隔ID）和CHROM/POS/REF/ALT检查，均未出现在该版VCF；两套各5条 `miss parent interpreted variant` 的子记录却全部仍在，而报告关联的3个父ID未在VCF中。例如1298290仍有自己的分类与疾病字段。

因此保留该文件，用于解释VCF覆盖缺口、追查关联关系和安排个别记录回查；不能仅凭进入error.txt就删除某变异，也不能把VCF查不到理解为ClinVar没有记录。[官方VCF说明](https://ftp.ncbi.nlm.nih.gov/pub/clinvar/README_VCF.txt)区分单变异与组合解读，`CLNSIGINCL`表示组合分类。父记录缺失的具体内部原因未被本次核实，不从日志名称进一步猜测。角色元数据已从过窄的 `upstream_vcf_exclusion_report` 修正为 `upstream_vcf_issue_report`，原始文件不改动。

## 获取、登记与校验

在项目目录执行，Python 3.9+ 与 curl 即可。默认单路最高 2 MiB/s，不改变文献采集及模型下载配置。两个获取脚本共用下载锁，避免并行叠加限速；遇临时网络错误由 curl 有限重试，未完成内容保留 `.part`。

```bash
python3 -B scripts/fetch_refseq_annotations.py --with-fasta
python3 -B scripts/fetch_clinvar_snapshot.py
python3 -B scripts/manage_reference_files.py list
python3 -B scripts/manage_reference_files.py verify
```

版本在获取脚本中显式固定，不静默切到 `latest/current`。重复执行从已校验 staging 复用；只登记完整通过的文件。RefSeq/ClinVar VCF 使用官方 MD5 及本地 SHA256，压缩文件另核验 CRC。ClinVar 的 TBI 没有独立官方 MD5，保存本地 SHA256 并检查压缩完整性和 TBI 格式，不伪称上游校验通过。

其他已有文件通过 JSON 计划登记：

```bash
python3 -B scripts/manage_reference_files.py import --plan /path/import-plan.json
```

计划的 `entries` 含 `source_path`、`relative_path`、`provider`、`dataset`、`assembly`、`assembly_accession`、`annotation_release`、`source_url`、`retrieved_at`、`upstream_md5`，补充来源放 `extra`。未知时间/注释版本允许 null，不能猜测。登记工具先核验整个计划，使用文件锁、拒绝越界与覆盖，再原子更新 manifest。

## 原有资源与分发

旧 ANNOVAR `refGeneWithVer` TXT/FASTA 仍保留生产原路径及挂载，版本未知的明确登记未知。它们与 NCBI 官方 GFF 分开，不因名称包含 RefSeq 就混成同一来源。现有冻结备份不迁移、不清理。

本批建立本地版本库，尚未上传115、配置定期更新或对客户发布。后续115分发按原目录携带 manifest/校验值；客户端先核对版本和 SHA256，再决定下载，下载完成后再验证。只同步选定原始文件与公开说明，不同步 `.staging`、私有会话、生产数据库及本机路径清单。115直连下载由独立 `scripts/download_115.py` 负责。

记录[NCBI使用政策](https://www.ncbi.nlm.nih.gov/home/about/policies/)，并保留第三方权利说明；不能把 ANNOVAR/COSMIC 等其他来源自动归入同一公开分发权限。后续候选为 HGNC/NCBI Gene、Ensembl/GENCODE；gnomAD、dbSNP、VEP cache 等先确认用途和版本再扩充。
