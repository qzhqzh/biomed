import { AppShell } from "@/layouts/AppShell";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";

const ENDPOINTS = [
  {
    method: "GET",
    path: "/api/v1/curation/overview/",
    desc: "正式知识库统计、来源版本与工作流状态",
  },
  {
    method: "GET / POST",
    path: "/api/v1/curation/sources/",
    desc: "来源版本记录 / 提交 MONDO、RXNORM 更新任务",
  },
  {
    method: "GET",
    path: "/api/v1/curation/concepts/?namespace=MONDO&q=lung",
    desc: "当前完整版本中的标准疾病或药物成分",
  },
  {
    method: "GET / POST",
    path: "/api/v1/curation/materials/",
    desc: "资料列表 / 合法 PDF 上传（multipart，最大 20 MB）",
  },
  {
    method: "GET",
    path: "/api/v1/curation/versions/{id}/processing/",
    desc: "当前原文件版本的解析、抽取状态与分页任务记录；只读，不创建任务",
  },
  {
    method: "POST",
    path: "/api/v1/curation/pubmed/import/",
    desc: "按 PMID 创建元数据、PMC 全文获取及解析工作流",
  },
  {
    method: "GET",
    path: "/api/v1/curation/workflows/",
    desc: "持久任务、结果与失败原因",
  },
  {
    method: "GET / POST",
    path: "/api/v1/curation/workflows/{id}/",
    desc: "执行历史 / 重试失败、待处理或待配置任务",
  },
  {
    method: "GET",
    path: "/api/v1/curation/evidence/",
    desc: "正式证据，默认只返回已发布记录；教学样例不混入",
  },
  {
    method: "GET / POST",
    path: "/api/v1/curation/evidence/{id}/",
    desc: "原文及审核详情 / 具名审阅者确认或撤回（需权限）",
  },
  {
    method: "GET / POST / DELETE",
    path: "/api/v1/curation/session/",
    desc: "查询审阅身份 / 登录 / 退出；校验 CSRF",
  },
  {
    method: "GET",
    path: "/api/v1/knowledge/genes/?q=EGFR",
    desc: "基因或转录本主编号检索；不核验输入的 accession 版本",
  },
  {
    method: "GET",
    path: "/api/v1/knowledge/transcripts/",
    desc: "本地 MANE 转录本映射",
  },
  {
    method: "GET",
    path: "/api/v1/evidence/catalog/",
    desc: "教学资料目录与出处",
  },
  {
    method: "GET",
    path: "/api/v1/evidence/cases/",
    desc: "虚构示例，非真实患者数据",
  },
  {
    method: "POST",
    path: "/api/v1/evidence/extract/",
    desc: "提取候选字段；仅使用有限词表",
  },
  {
    method: "POST",
    path: "/api/v1/evidence/search/",
    desc: "确认字段后查询；confirmed=true，未知条件保持未知",
  },
  {
    method: "GET / POST",
    path: "/api/v1/evidence/issues/",
    desc: "读取最近 200 条记录 / 创建问题",
  },
  {
    method: "PATCH",
    path: "/api/v1/evidence/issues/{id}/",
    desc: "更新处理状态与经验",
  },
  {
    method: "GET",
    path: "/api/v1/evidence/issues/export/",
    desc: "完整导出所有问题与经验",
  },
  {
    method: "GET",
    path: "/health/live",
    desc: "进程存活探测",
  },
  {
    method: "GET",
    path: "/health/ready",
    desc: "引擎与参考数据就绪状态",
  },
  {
    method: "GET",
    path: "/api/v1/engines/",
    desc: "列出 VEP / ANNOVAR 能力与 readiness",
  },
  {
    method: "POST",
    path: "/api/v1/annotations/",
    desc: "同步注释：engine=vep|annovar|both，assembly，variants[]",
  },
  {
    method: "POST",
    path: "/api/v1/jobs/",
    desc: "创建后台注释任务：engines[]，assembly，variants[]（10 秒内同客户端仅一次）",
  },
  {
    method: "GET",
    path: "/api/v1/jobs/",
    desc: "任务历史列表",
  },
  {
    method: "GET",
    path: "/api/v1/jobs/{id}/",
    desc: "查询单个任务状态与结果",
  },
];

export default function DocsApp() {
  return (
    <AppShell
      title="API 说明"
      description="统一入口的生信与资料 API。注释接口沿用 API Key 设置；资料写入需同源请求和 CSRF 校验；人工抽查另需审阅账号。"
      crumbs={[{ label: "API 说明" }]}
    >
      <div className="grid gap-4 lg:grid-cols-[1.2fr_1fr]">
        <Card>
          <CardHeader>
            <CardTitle>Endpoints</CardTitle>
            <CardDescription>
              浏览器与程序化调用共用同一套路径。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {ENDPOINTS.map((item, index) => (
              <div key={`${item.method}-${item.path}`}>
                {index > 0 ? <Separator className="mb-4" /> : null}
                <div className="flex flex-wrap items-baseline gap-2">
                  <code className="rounded bg-muted px-1.5 py-0.5 text-xs font-semibold">
                    {item.method}
                  </code>
                  <code className="break-all text-sm">{item.path}</code>
                </div>
                <p className="mt-1 text-sm text-muted-foreground">
                  {item.desc}
                </p>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>注释请求示例</CardTitle>
            <CardDescription>
              生产环境请配置 <code>BIOTOOLS_API_KEYS</code>，并带上{" "}
              <code>X-API-Key</code>。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <pre className="overflow-x-auto rounded-lg border bg-muted/40 p-4 text-xs leading-relaxed">
              {`curl -X POST http://localhost:50011/api/v1/annotations/ \\
  -H 'Content-Type: application/json' \\
  -H 'X-API-Key: YOUR_KEY' \\
  -d '{
    "engine": "vep",
    "assembly": "GRCh37",
    "variants": ["17:43092951 G>A"]
  }'`}
            </pre>
            <p className="text-sm text-muted-foreground">
              ANNOVAR 默认关闭（<code>ANNOVAR_PUBLIC_ENABLED=0</code>
              ），确认授权后再开启。部署细节见仓库 <code>docs/DEPLOY.md</code>。
            </p>
          </CardContent>
        </Card>
      </div>
    </AppShell>
  );
}
