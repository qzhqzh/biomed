import { type FormEvent, useEffect, useState } from "react";
import {
  ArrowRight,
  BookOpen,
  Database,
  Dna,
  FileCheck2,
  FileText,
  Pill,
  Search,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import { AppShell } from "@/layouts/AppShell";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { fetchKnowledgeMeta, type KnowledgeMeta } from "@/lib/api";
import { useCuration, type Overview } from "@/lib/curation";
import { ErrorMessage } from "@/components/curation/Common";

function Entry({
  icon: Icon,
  title,
  description,
  meta,
  href,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  meta?: string;
  href: string;
}) {
  return (
    <a
      href={href}
      className="group flex min-h-24 items-start gap-4 border-b py-5 transition-colors hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-ring"
    >
      <Icon className="mt-1 size-5 shrink-0" />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <h3 className="text-base font-semibold">{title}</h3>
        <p className="text-sm/relaxed text-muted-foreground">{description}</p>
        {meta && <p className="mt-1 text-xs text-muted-foreground">{meta}</p>}
      </div>
      <ArrowRight className="mt-1 size-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-1" />
    </a>
  );
}

export default function HomeApp() {
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState("gene");
  const [meta, setMeta] = useState<KnowledgeMeta | null>(null);
  const [metaError, setMetaError] = useState(false);
  const overview = useCuration<Overview>("overview/");
  useEffect(() => {
    let active = true;
    fetchKnowledgeMeta()
      .then((data) => {
        if (active) setMeta(data);
      })
      .catch(() => {
        if (active) setMetaError(true);
      });
    return () => {
      active = false;
    };
  }, []);
  function search(event: FormEvent) {
    event.preventDefault();
    window.location.href =
      scope === "gene"
        ? `/knowledge/genes/?q=${encodeURIComponent(query.trim())}`
        : `/knowledge/${scope === "RXNORM" ? "drugs" : "diseases"}/?q=${encodeURIComponent(query.trim())}`;
  }
  const mondo = overview.data?.sources.find(
    (source) => source.namespace === "MONDO",
  );
  const rxnorm = overview.data?.sources.find(
    (source) => source.namespace === "RXNORM",
  );
  return (
    <AppShell
      title="生信与医学知识库"
      description="以真实标准实体为基础，把论文与指南中的知识连接到可追溯的临床证据。"
      crumbs={[{ label: "总览" }]}
      actions={
        <Button asChild variant="outline" className="h-9">
          <a href="/evidence/sources/">
            添加资料
            <ArrowRight />
          </a>
        </Button>
      }
    >
      <section
        className="flex flex-col gap-5 rounded-lg bg-muted/55 p-5 md:p-7"
        aria-labelledby="entity-search-title"
      >
        <div className="flex flex-col gap-2">
          <h2 id="entity-search-title" className="text-xl font-semibold">
            从一个标准实体开始
          </h2>
          <p className="text-sm text-muted-foreground">
            查清名称、ID 与来源版本，再连接资料和证据。
          </p>
        </div>
        <form
          onSubmit={search}
          className="flex flex-col gap-3 sm:flex-row sm:items-end"
        >
          <Field className="sm:w-40">
            <FieldLabel htmlFor="entity-kind">实体类型</FieldLabel>
            <Select value={scope} onValueChange={setScope}>
              <SelectTrigger
                id="entity-kind"
                className="data-[size=default]:h-11 w-full text-sm"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="gene">基因</SelectItem>
                <SelectItem value="MONDO">疾病</SelectItem>
                <SelectItem value="RXNORM">药物成分</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field className="flex-1">
            <FieldLabel htmlFor="home-gene-query">名称或标准 ID</FieldLabel>
            <Input
              id="home-gene-query"
              className="h-11 text-sm"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={
                scope === "gene"
                  ? "例如 EGFR、672、NM_005228"
                  : scope === "MONDO"
                    ? "例如 lung cancer、MONDO:0008903"
                    : "例如 osimertinib"
              }
            />
          </Field>
          <Button type="submit" className="h-11 px-7 text-sm">
            <Search />
            查询
          </Button>
        </form>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
          <span className="text-muted-foreground">基因示例：</span>
          {["EGFR", "BRCA1", "NM_005228"].map((gene) => (
            <a
              key={gene}
              href={`/knowledge/genes/?q=${gene}`}
              className="text-primary underline underline-offset-4"
            >
              {gene}
            </a>
          ))}
        </div>
      </section>
      <ErrorMessage error={overview.error} />
      <div className="grid gap-8 lg:grid-cols-2 lg:gap-10">
        <section aria-labelledby="standards-title">
          <h2 id="standards-title" className="pb-3 text-xl font-semibold">
            知识库
          </h2>
          <Separator />
          <Entry
            icon={Dna}
            title="基因"
            description="基因名称、标识与转录本对应关系"
            meta={
              meta?.gene_loaded
                ? `${meta.gene_loaded.toLocaleString()} 条基因记录 · NCBI Gene / MANE ${meta.mane?.version ?? ""}`
                : metaError
                  ? "来源状态暂不可读"
                  : "正在读取来源版本…"
            }
            href="/knowledge/genes/"
          />
          <Entry
            icon={BookOpen}
            title="疾病"
            description="疾病名称、同义词与分类关系"
            meta={
              mondo
                ? `${mondo.count.toLocaleString()} 个实体 · MONDO ${mondo.version}`
                : "MONDO · 版本与更新记录见标准库"
            }
            href="/knowledge/diseases/"
          />
          <Entry
            icon={Pill}
            title="药物"
            description="成分标准名称与 RxNorm 标识"
            meta={
              rxnorm
                ? `${rxnorm.count.toLocaleString()} 个实体 · ${rxnorm.version}`
                : "RxNorm IN · 不等同于适应症库"
            }
            href="/knowledge/drugs/"
          />
        </section>
        <section aria-labelledby="workflow-title">
          <h2 id="workflow-title" className="pb-3 text-xl font-semibold">
            证据库
          </h2>
          <Separator />
          <Entry
            icon={FileCheck2}
            title="临床证据"
            description="AI 审核通过后发布，人工抽查可确认或撤回"
            meta={
              overview.data
                ? `${overview.data.evidence.published ?? 0} 条已发布证据 · 原文可追溯`
                : "正在读取证据状态…"
            }
            href="/evidence/clinical/"
          />
          <Entry
            icon={FileText}
            title="文献管理"
            description="获取 PubMed / PMC 文献，保存论文原文与版本"
            href="/evidence/sources/papers/"
          />
          <Entry
            icon={BookOpen}
            title="指南管理"
            description="指南归档、解析方案和按文件版本追溯的处理记录"
            meta="先定义知识结构，再安排指南解析"
            href="/evidence/sources/guidelines/"
          />
        </section>
      </div>
      <section className="flex flex-wrap items-center justify-between gap-4 border-t pt-5">
        <div className="flex flex-col gap-2">
          <h2 className="text-base font-semibold">生信分析与管理</h2>
          <p className="text-sm text-muted-foreground">
            生信工具支撑分析，工作流统一管理资料处理和执行记录。
          </p>
        </div>
        <div className="flex flex-wrap gap-3">
          <Button asChild variant="outline" className="h-10">
            <a href="/management/workflows/">
              <Workflow />
              工作流
            </a>
          </Button>
          <Button asChild variant="outline" className="h-10">
            <a href="/tools/annotate/">变异注释</a>
          </Button>
          <Button asChild variant="outline" className="h-10">
            <a href="/tools/data-updates/">
              <Database />
              基因数据更新
            </a>
          </Button>
        </div>
      </section>
    </AppShell>
  );
}
