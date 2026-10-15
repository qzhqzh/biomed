import { useState, type FormEvent } from "react";
import { FileText, Search, Upload, Download, ArrowRight } from "lucide-react";
import { AppShell } from "@/layouts/AppShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field, FieldLabel } from "@/components/ui/field";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import { MaterialProcessing } from "@/components/curation/MaterialProcessing";
import { ErrorMessage, NoData, Status } from "@/components/curation/Common";
import {
  curation,
  date,
  useCuration,
  KINDS,
  type Material,
  type MaterialDetail,
  type Work,
} from "@/lib/curation";

type Article = { pmid: string; title: string; journal: string; date: string };
const CATEGORIES = {
  all: "全部",
  paper: "文献",
  guideline: "指南",
  other: "其他",
};

export default function LibraryApp() {
  const [category, setCategory] = useState(() => {
    const value =
      location.pathname === "/evidence/sources/guidelines/"
        ? "guideline"
        : location.pathname === "/evidence/sources/papers/"
          ? "paper"
          : (new URLSearchParams(location.search).get("kind") ?? "all");
    return Object.hasOwn(CATEGORIES, value) ? value : "all";
  });
  const [selected, setSelected] = useState<string | null>(() =>
    new URLSearchParams(location.search).get("material"),
  );
  const [version, setVersion] = useState(
    new URLSearchParams(location.search).get("version") ?? "",
  );
  const [passageOffset, setPassageOffset] = useState(0);
  const [offset, setOffset] = useState(0);
  const [query, setQuery] = useState("");
  const [pmid, setPmid] = useState("");
  const [searchId, setSearchId] = useState<string | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadMaterial, setUploadMaterial] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState("paper");
  const [startParse, setStartParse] = useState(true);
  const [rights, setRights] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const list = useCuration<{
    total: number;
    counts: Record<string, number>;
    results: Material[];
  }>(
    `materials/?offset=${offset}${category !== "all" ? `&kind=${category}` : ""}`,
    7000,
  );
  const detail = useCuration<MaterialDetail>(
    selected
      ? `materials/${selected}/?offset=${passageOffset}${version ? `&version=${version}` : ""}`
      : null,
    7000,
  );
  const searchJob = useCuration<Work>(
    searchId ? `workflows/${searchId}/` : null,
    3000,
  );
  const articles = (searchJob.data?.result?.articles ?? []) as Article[];
  function changeCategory(value: string) {
    setCategory(value);
    setOffset(0);
    const url = new URL(location.href);
    url.pathname =
      value === "guideline"
        ? "/evidence/sources/guidelines/"
        : value === "paper"
          ? "/evidence/sources/papers/"
          : "/evidence/sources/";
    if (value === "other") url.searchParams.set("kind", value);
    else url.searchParams.delete("kind");
    document.title = `${value === "guideline" ? "指南管理" : value === "paper" ? "文献管理" : "文本资料"} · biomed`;
    history.replaceState(null, "", url);
  }
  function openMaterial(material: Material | null) {
    setSelected(material?.id ?? null);
    setVersion("");
    setPassageOffset(0);
    const url = new URL(location.href);
    url.searchParams.delete("version");
    if (material) url.searchParams.set("material", material.id);
    else url.searchParams.delete("material");
    history.replaceState(null, "", url);
  }
  async function importPmid(value: string) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const job = await curation<Work>("pubmed/import/", {
        method: "POST",
        body: JSON.stringify({ pmid: value.trim() }),
      });
      setNotice(
        job.state === "succeeded"
          ? "此文献已经入库。"
          : "已创建获取任务；系统将保存元数据并检查可复用的 PMC 全文。",
      );
      list.refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function search(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setSearchId(null);
    try {
      const job = await curation<Work>("pubmed/search/", {
        method: "POST",
        body: JSON.stringify({ query: query.trim() }),
      });
      setSearchId(job.id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function upload(event: FormEvent) {
    event.preventDefault();
    if (!file || !confirmed) return;
    setBusy(true);
    setError("");
    const data = new FormData();
    data.set("file", file);
    data.set("title", title);
    data.set("kind", kind);
    data.set("rights", rights);
    data.set("rights_confirmed", "true");
    data.set("start_parse", String(startParse));
    if (uploadMaterial) data.set("material_id", uploadMaterial);
    try {
      const result = await curation<{ material: Material }>("materials/", {
        method: "POST",
        body: data,
      });
      setUploadOpen(false);
      openMaterial(result.material);
      setNotice(
        startParse
          ? "PDF 已保存，已提交解析任务；执行状态请查看工作流。"
          : "PDF 已归档，原文件已保留；本次未启动解析或 AI 抽取。",
      );
      changeCategory(result.material.kind);
      setFile(null);
      setConfirmed(false);
      list.refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function startUpload(material?: Material) {
    const uploadKind =
      material?.kind ?? (category === "all" ? "paper" : category);
    setUploadMaterial(material?.id ?? null);
    setTitle(material?.title ?? "");
    setKind(uploadKind);
    setStartParse(uploadKind === "paper");
    setConfirmed(false);
    setFile(null);
    setRights("");
    setError("");
    setUploadOpen(true);
  }
  const selectedVersion = detail.data?.versions.find(
    (v) => v.id === detail.data?.selected_version,
  );
  return (
    <AppShell
      title={
        category === "guideline"
          ? "指南管理"
          : category === "paper"
            ? "文献管理"
            : "文本资料"
      }
      description={
        category === "guideline"
          ? "管理指南原文、来源版本和解析记录。先定义知识结构，再开展解析。"
          : "管理证据的文本来源：文献、指南和其他资料。原文与版本可追溯。"
      }
      crumbs={[
        { label: "证据库" },
        { label: "文本资料", href: "/evidence/sources/" },
        ...(category === "guideline"
          ? [{ label: "指南管理" }]
          : category === "paper"
            ? [{ label: "文献管理" }]
            : []),
      ]}
      actions={
        <Button type="button" className="h-9" onClick={() => startUpload()}>
          <Upload />
          上传 PDF
        </Button>
      }
    >
      <ErrorMessage error={error || list.error} />
      <Tabs value={category} onValueChange={changeCategory}>
        <TabsList aria-label="资料分类">
          {Object.entries(CATEGORIES).map(([value, label]) => (
            <TabsTrigger key={value} value={value}>
              {label}
              {list.data ? `（${list.data.counts[value] ?? 0}）` : ""}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value={category} className="mt-4 flex flex-col gap-5">
          {category === "guideline" && (
            <section
              className="rounded-lg bg-muted/50 p-5"
              aria-label="指南处理安排"
            >
              <Badge variant="outline">待定义知识结构</Badge>
              <h2 className="mt-3 text-base font-semibold">
                指南先归档，解析方案待确认
              </h2>
              <p className="mt-2 text-sm/relaxed text-muted-foreground">
                打开指南可查看原文件、解析方案与真实处理记录。当前上传指南仅保存原文，待知识结构确定后再安排代表页试验与分章节处理。
              </p>
            </section>
          )}
          {(category === "all" || category === "paper") && (
            <section
              className="rounded-lg bg-muted/50 p-5 md:p-6"
              aria-label="获取文献"
            >
              <Tabs defaultValue="pmid">
                <TabsList>
                  <TabsTrigger value="pmid">按 PMID 导入</TabsTrigger>
                  <TabsTrigger value="search">检索 PubMed</TabsTrigger>
                </TabsList>
                <TabsContent value="pmid" className="mt-5">
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      void importPmid(pmid);
                    }}
                    className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-end"
                  >
                    <Field className="flex-1">
                      <FieldLabel htmlFor="pmid">PubMed ID</FieldLabel>
                      <Input
                        id="pmid"
                        className="h-10 text-sm"
                        value={pmid}
                        onChange={(e) => setPmid(e.target.value)}
                        inputMode="numeric"
                        placeholder="输入 PMID"
                        required
                        pattern="[0-9]{1,10}"
                      />
                    </Field>
                    <Button disabled={busy} className="h-10 px-5 text-sm">
                      获取文献
                    </Button>
                  </form>
                </TabsContent>
                <TabsContent value="search" className="mt-5">
                  <form
                    onSubmit={search}
                    className="flex flex-col gap-3 sm:flex-row sm:items-end"
                  >
                    <Field className="flex-1">
                      <FieldLabel htmlFor="pubmed-query">
                        PubMed 检索式
                      </FieldLabel>
                      <Input
                        id="pubmed-query"
                        className="h-10 text-sm"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        placeholder="例如 EGFR AND lung cancer"
                        required
                        maxLength={500}
                      />
                    </Field>
                    <Button disabled={busy} className="h-10 px-5 text-sm">
                      <Search />
                      检索
                    </Button>
                  </form>
                  {searchJob.data && (
                    <div className="mt-4 flex items-center gap-3 text-sm">
                      <Status value={searchJob.data.state} />
                      {searchJob.data.message ||
                        (searchJob.data.state === "succeeded"
                          ? `找到 ${String(searchJob.data.result.total ?? 0)} 条，展示最多 10 条`
                          : "正在通过官方接口检索，可离开本页。")}
                    </div>
                  )}
                  <ErrorMessage error={searchJob.error} />
                  {articles.length > 0 && (
                    <div className="mt-5 divide-y border-t">
                      {articles.map((article) => (
                        <div
                          key={article.pmid}
                          className="flex flex-col items-start gap-3 py-4 sm:flex-row"
                        >
                          <div className="min-w-0 flex-1">
                            <a
                              href={`https://pubmed.ncbi.nlm.nih.gov/${article.pmid}/`}
                              target="_blank"
                              rel="noreferrer"
                              className="text-sm font-medium leading-relaxed hover:underline"
                            >
                              {article.title}
                            </a>
                            <p className="mt-1 text-xs text-muted-foreground">
                              PMID {article.pmid} · {article.journal} ·{" "}
                              {article.date}
                            </p>
                          </div>
                          <Button
                            type="button"
                            variant="outline"
                            disabled={busy}
                            className="h-9"
                            onClick={() => importPmid(article.pmid)}
                          >
                            导入这篇
                          </Button>
                        </div>
                      ))}
                    </div>
                  )}
                </TabsContent>
              </Tabs>
              <p className="mt-4 text-sm/relaxed text-muted-foreground">
                自动获取 PubMed 元数据和 PMC
                允许复用的全文。无法获取全文时，可补充合法持有的
                PDF；扫描件当前需要另行 OCR。
              </p>
            </section>
          )}
          {notice && (
            <p role="status" className="text-sm leading-relaxed">
              {notice}{" "}
              <a
                href="/management/workflows/"
                className="text-primary underline"
              >
                查看工作流
              </a>
            </p>
          )}
          <div className="flex items-center justify-between">
            <h2 className="text-xl font-semibold">已入库资料</h2>
            <span className="text-sm text-muted-foreground">
              {list.data ? `${list.data.total} 份资料` : "读取中…"}
            </span>
          </div>
          {list.data?.results.length ? (
            <div className="divide-y border-y">
              {list.data.results.map((material) => (
                <button
                  key={material.id}
                  type="button"
                  className="flex w-full items-start gap-4 py-5 text-left hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-ring"
                  onClick={() => {
                    openMaterial(material);
                  }}
                >
                  <FileText className="mt-1 size-5 shrink-0 text-muted-foreground" />
                  <div className="min-w-0 flex-1">
                    <h3 className="break-words text-sm font-semibold leading-relaxed">
                      {material.title}
                    </h3>
                    <p className="mt-2 text-xs text-muted-foreground">
                      {KINDS[material.kind]} ·{" "}
                      {material.identifiers.pubmed
                        ? `PMID ${material.identifiers.pubmed} · `
                        : ""}
                      {date(material.created_at)}
                    </p>
                  </div>
                  <ArrowRight className="mt-1 size-4 shrink-0" />
                </button>
              ))}
            </div>
          ) : (
            !list.loading && (
              <NoData
                title={
                  category === "all"
                    ? "从第一份真实资料开始"
                    : `还没有${CATEGORIES[category as keyof typeof CATEGORIES]}资料`
                }
                description="上传合法持有的 PDF，可选择先归档。文献也可以通过 PMID 导入；保存原文不会直接发布证据。"
              />
            )
          )}
          {list.data && list.data.total > 50 && (
            <div className="flex justify-between">
              <Button
                variant="outline"
                disabled={!offset}
                onClick={() => setOffset(offset - 50)}
              >
                上一页
              </Button>
              <Button
                variant="outline"
                disabled={offset + 50 >= list.data.total}
                onClick={() => setOffset(offset + 50)}
              >
                下一页
              </Button>
            </div>
          )}
        </TabsContent>
      </Tabs>
      <Sheet
        open={Boolean(selected)}
        onOpenChange={(open) => {
          if (!open) openMaterial(null);
        }}
      >
        <SheetContent className="data-[side=right]:w-full overflow-y-auto data-[side=right]:sm:max-w-2xl">
          <SheetHeader>
            <SheetTitle>{detail.data?.title ?? "资料详情"}</SheetTitle>
            <SheetDescription>
              原文件、解析方案与处理记录均对应具体资料版本。归档和解析不等于证据发布。
            </SheetDescription>
          </SheetHeader>
          <div className="flex flex-col gap-5 p-5 text-sm">
            <ErrorMessage error={detail.error} />
            {detail.data && (
              <>
                <div className="flex flex-wrap gap-2">
                  <Badge variant="outline">{KINDS[detail.data.kind]}</Badge>
                  {Object.entries(detail.data.identifiers).map(
                    ([key, value]) => (
                      <Badge key={key} variant="secondary">
                        {key.toUpperCase()} {value}
                      </Badge>
                    ),
                  )}
                </div>
                <Field>
                  <FieldLabel htmlFor="material-version">资料版本</FieldLabel>
                  <Select
                    value={detail.data.selected_version ?? ""}
                    onValueChange={(value) => {
                      setVersion(value);
                      setPassageOffset(0);
                      const url = new URL(location.href);
                      url.searchParams.set("version", value);
                      history.replaceState(null, "", url);
                    }}
                  >
                    <SelectTrigger
                      id="material-version"
                      className="data-[size=default]:h-10 w-full text-sm"
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {detail.data.versions.map((v) => (
                        <SelectItem key={v.id} value={v.id}>
                          {v.integrity_status === "quarantined"
                            ? "[已隔离] "
                            : ""}
                          {v.media_type === "application/pubmed+xml"
                            ? "PubMed 元数据"
                            : v.media_type === "application/pdf"
                              ? "PDF"
                              : "PMC 全文"}{" "}
                          · {date(v.acquired_at)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>
                <Tabs
                  key={detail.data.id}
                  defaultValue={
                    detail.data.kind === "guideline" ? "plan" : "source"
                  }
                >
                  <TabsList aria-label="资料详情">
                    <TabsTrigger value="source">原文与版本</TabsTrigger>
                    <TabsTrigger value="plan">
                      {detail.data.kind === "guideline"
                        ? "解析方案"
                        : "解析状态"}
                    </TabsTrigger>
                    <TabsTrigger value="history">处理记录</TabsTrigger>
                  </TabsList>
                  {selectedVersion && (
                    <>
                      <TabsContent value="plan">
                        <MaterialProcessing
                          key={`plan-${selectedVersion.id}`}
                          version={selectedVersion}
                          guideline={detail.data.kind === "guideline"}
                          mode="plan"
                        />
                      </TabsContent>
                      <TabsContent value="history">
                        <MaterialProcessing
                          key={`history-${selectedVersion.id}`}
                          version={selectedVersion}
                          guideline={detail.data.kind === "guideline"}
                          mode="history"
                        />
                      </TabsContent>
                    </>
                  )}
                  {!selectedVersion && (
                    <p className="mt-4 text-sm text-muted-foreground">
                      请选择一个资料版本查看状态与记录。
                    </p>
                  )}
                  <TabsContent
                    value="source"
                    className="mt-5 flex flex-col gap-5"
                  >
                    {selectedVersion && (
                      <>
                        {selectedVersion.integrity_status === "quarantined" && (
                          <ErrorMessage
                            error={`该版本已隔离，不能用于提取证据。${selectedVersion.integrity_note}`}
                          />
                        )}
                        <div className="space-y-2 border-y py-4">
                          <h3 className="font-semibold">使用权利与出处</h3>
                          <p className="break-words text-muted-foreground">
                            原文件：{selectedVersion.filename}
                          </p>
                          <p className="break-words text-sm/relaxed">
                            {selectedVersion.rights}
                          </p>
                          {selectedVersion.source_url && (
                            <a
                              href={selectedVersion.source_url}
                              target="_blank"
                              rel="noreferrer"
                              className="block text-primary underline"
                            >
                              查看官方来源
                            </a>
                          )}
                          <p className="break-all text-xs text-muted-foreground">
                            SHA-256：{selectedVersion.sha256}
                          </p>
                        </div>
                        <div className="flex flex-wrap gap-3">
                          <Button asChild variant="outline" className="h-10">
                            <a
                              href={`/api/v1/curation/versions/${selectedVersion.id}/download/`}
                            >
                              <Download />
                              下载原文件
                            </a>
                          </Button>
                          <Button
                            variant="outline"
                            className="h-10"
                            onClick={() => startUpload(detail.data!)}
                          >
                            补充 PDF 版本
                          </Button>
                        </div>
                      </>
                    )}
                    <h3 className="font-semibold">
                      {selectedVersion?.integrity_status === "quarantined"
                        ? "隔离原文（不可用于证据）"
                        : "可引用文本"}{" "}
                      · {detail.data.passage_total} 个定位块
                    </h3>
                    {detail.data.passages.length ? (
                      detail.data.passages.map((p) => (
                        <article key={p.id} className="border-b pb-5">
                          <p className="mb-2 break-all text-xs font-medium text-primary">
                            {p.locator}
                          </p>
                          <p className="whitespace-pre-wrap break-words text-sm/relaxed">
                            {p.text}
                          </p>
                          <p className="mt-2 text-xs text-muted-foreground">
                            解析版本：{p.parser_version}
                          </p>
                        </article>
                      ))
                    ) : (
                      <div className="flex flex-col gap-3">
                        {selectedVersion?.media_type === "application/pdf" &&
                        !detail.data.parse_job ? (
                          <>
                            <p className="font-medium">
                              已归档，尚未启动文本解析。
                            </p>
                            <p className="leading-relaxed text-muted-foreground">
                              原文件可下载查看。长指南需先确定章节、图表与脚注的解析方式及使用范围，再进行处理。
                            </p>
                          </>
                        ) : (
                          <>
                            {detail.data.parse_job && (
                              <Status value={detail.data.parse_job.state} />
                            )}
                            <p className="leading-relaxed text-muted-foreground">
                              {detail.data.parse_job?.message ||
                                "当前版本还没有可引用文本。元数据不作为全文；解析进度及失败原因请查看工作流。"}
                            </p>
                          </>
                        )}
                      </div>
                    )}
                    {detail.data.passage_total > 50 && (
                      <div className="flex justify-between">
                        <Button
                          variant="outline"
                          disabled={!passageOffset}
                          onClick={() => setPassageOffset(passageOffset - 50)}
                        >
                          上一页文本
                        </Button>
                        <Button
                          variant="outline"
                          disabled={
                            passageOffset + 50 >= detail.data.passage_total
                          }
                          onClick={() => setPassageOffset(passageOffset + 50)}
                        >
                          下一页文本
                        </Button>
                      </div>
                    )}
                    <a
                      href="/management/workflows/"
                      className="text-primary underline"
                    >
                      查看工作流
                    </a>
                  </TabsContent>
                </Tabs>
              </>
            )}
          </div>
        </SheetContent>
      </Sheet>
      <Sheet
        open={uploadOpen}
        onOpenChange={(open) => {
          if (!busy) setUploadOpen(open);
        }}
      >
        <SheetContent className="data-[side=right]:w-full overflow-y-auto data-[side=right]:sm:max-w-lg">
          <SheetHeader>
            <SheetTitle>
              {uploadMaterial ? "补充 PDF 版本" : "上传文本资料"}
            </SheetTitle>
            <SheetDescription>
              论文、指南和其他文本共用同一套来源与版本记录。
            </SheetDescription>
          </SheetHeader>
          <form onSubmit={upload} className="flex flex-col gap-5 p-5">
            <ErrorMessage error={error} />
            <Field>
              <FieldLabel htmlFor="material-title">资料标题</FieldLabel>
              <Input
                id="material-title"
                disabled={Boolean(uploadMaterial)}
                className="h-10 text-sm"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                required
                maxLength={2000}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="material-kind">资料类型</FieldLabel>
              <Select
                value={kind}
                onValueChange={(value) => {
                  setKind(value);
                  setStartParse(value === "paper");
                }}
                disabled={Boolean(uploadMaterial)}
              >
                <SelectTrigger
                  id="material-kind"
                  className="data-[size=default]:h-10 w-full text-sm"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {["paper", "guideline", "other"].map((k) => (
                    <SelectItem key={k} value={k}>
                      {KINDS[k]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel htmlFor="pdf-file">
                PDF 文件（不超过 20 MB）
              </FieldLabel>
              <Input
                id="pdf-file"
                type="file"
                accept="application/pdf,.pdf"
                className="h-10"
                required
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="material-rights">使用权利说明</FieldLabel>
              <Textarea
                id="material-rights"
                value={rights}
                onChange={(e) => setRights(e.target.value)}
                placeholder="注明开放许可、授权范围或合法持有依据"
                required
                maxLength={4000}
              />
            </Field>
            {kind !== "guideline" && (
              <Field orientation="horizontal">
                <Checkbox
                  id="start-parse"
                  checked={startParse}
                  onCheckedChange={(value) => setStartParse(value === true)}
                />
                <FieldLabel htmlFor="start-parse" className="text-sm/relaxed">
                  保存后启动文本解析
                </FieldLabel>
              </Field>
            )}
            <p className="text-sm/relaxed text-muted-foreground">
              {kind === "guideline"
                ? "指南当前仅归档。知识结构确定后，再选择解析范围和处理方式；此次保存不创建解析或 AI 抽取任务。"
                : startParse
                  ? "按页提取文字；复杂图表和扫描件需要进一步处理。后续知识抽取仍取决于模型与资料使用范围。"
                  : "仅保存原文件，不创建解析或 AI 抽取任务。指南默认先归档。"}
            </p>
            <Field orientation="horizontal">
              <Checkbox
                id="rights-confirm"
                checked={confirmed}
                onCheckedChange={(value) => setConfirmed(value === true)}
              />
              <FieldLabel htmlFor="rights-confirm" className="text-sm/relaxed">
                我确认有权按所选方式保存或处理这份资料
              </FieldLabel>
            </Field>
            <Button
              disabled={busy || !confirmed || !file}
              className="h-10 text-sm"
            >
              {busy ? "保存中…" : startParse ? "保存并启动解析" : "保存原文"}
            </Button>
          </form>
        </SheetContent>
      </Sheet>
    </AppShell>
  );
}
