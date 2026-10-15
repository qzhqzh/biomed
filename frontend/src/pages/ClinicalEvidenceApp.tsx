import { useState, type FormEvent } from "react";
import { ArrowRight, Search, UserRound, LogOut } from "lucide-react";
import { AppShell } from "@/layouts/AppShell";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field, FieldLabel } from "@/components/ui/field";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { ErrorMessage, NoData, Status } from "@/components/curation/Common";
import {
  curation,
  date,
  useCuration,
  STATES,
  type ClinicalEvidence,
  type Session,
} from "@/lib/curation";

export default function ClinicalEvidenceApp() {
  const params = new URLSearchParams(location.search);
  const [input, setInput] = useState(
    params.get("q") ?? params.get("gene") ?? "",
  );
  const [query, setQuery] = useState(input);
  const [scope, setScope] = useState("published");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [signIn, setSignIn] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const list = useCuration<{ total: number; results: ClinicalEvidence[] }>(
    `evidence/?status=${scope}&q=${encodeURIComponent(query)}&offset=${offset}`,
  );
  const session = useCuration<Session>("session/");
  const detail = useCuration<ClinicalEvidence>(
    selected ? `evidence/${selected}/` : null,
  );
  async function authenticate(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await curation("session/", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      session.refresh();
      setSignIn(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setPassword("");
      setBusy(false);
    }
  }
  async function signOut() {
    setBusy(true);
    setError("");
    try {
      await curation("session/", { method: "DELETE" });
      session.refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function review(decision: string) {
    if (!selected) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await curation(`evidence/${selected}/`, {
        method: "POST",
        body: JSON.stringify({ decision, reason }),
      });
      detail.refresh();
      list.refresh();
      setReason("");
      setNotice(
        decision === "flag"
          ? "证据已撤回，来源与审核历史已保留。"
          : "人工核验结果已保存。",
      );
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const item = detail.data;
  return (
    <AppShell
      title="临床证据"
      description="每条证据都连接具体原文。AI 审核通过后发布，人工抽查结果独立保留。"
      crumbs={[{ label: "证据库" }, { label: "临床证据" }]}
      actions={
        session.data?.username ? (
          <div className="flex items-center gap-2">
            <span className="hidden text-sm sm:block">
              {session.data.username}
            </span>
            <Button
              variant="outline"
              className="h-9"
              onClick={signOut}
              disabled={busy}
            >
              <LogOut />
              退出
            </Button>
          </div>
        ) : (
          <Button
            variant="outline"
            className="h-9"
            onClick={() => {
              setError("");
              setSignIn(true);
            }}
          >
            <UserRound />
            审阅者登录
          </Button>
        )
      }
    >
      <div className="flex flex-wrap gap-x-8 gap-y-3 border-y py-4 text-sm">
        <span>发布条件：AI 审核通过</span>
        <span>人工状态：未抽查 / 已核验 / 发现问题</span>
        <span>问题处理：撤回并保留历史</span>
      </div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setQuery(input.trim());
          setOffset(0);
        }}
        className="flex flex-col gap-3 sm:flex-row sm:items-end"
      >
        <Field className="flex-1">
          <FieldLabel htmlFor="evidence-query">检索陈述或关联实体</FieldLabel>
          <Input
            id="evidence-query"
            className="h-10 text-sm"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="输入基因、疾病、药物名称或 ID"
          />
        </Field>
        <Field className="sm:w-40">
          <FieldLabel htmlFor="publication-filter">发布状态</FieldLabel>
          <Select
            value={scope}
            onValueChange={(value) => {
              setScope(value);
              setOffset(0);
            }}
          >
            <SelectTrigger
              id="publication-filter"
              className="data-[size=default]:h-10 w-full text-sm"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {[
                ["published", "已发布"],
                ["draft", "候选证据"],
                ["withdrawn", "已撤回"],
                ["all", "全部记录"],
              ].map(([value, label]) => (
                <SelectItem key={value} value={value}>
                  {label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Button className="h-10 px-5 text-sm">
          <Search />
          检索
        </Button>
      </form>
      <ErrorMessage error={error || list.error || session.error} />
      {notice && (
        <p role="status" className="text-sm">
          {notice}
        </p>
      )}
      {list.loading && (
        <p role="status" className="text-sm">
          正在读取证据…
        </p>
      )}
      {list.data?.results.length ? (
        <div className="divide-y border-y">
          {list.data.results.map((e) => (
            <button
              key={e.id}
              type="button"
              onClick={() => {
                setSelected(e.id);
                setReason("");
                setError("");
              }}
              className="flex w-full items-start gap-4 py-5 text-left hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-ring"
            >
              <div className="min-w-0 flex-1">
                <div className="mb-3 flex flex-wrap gap-2">
                  <Status value={e.publication_status} />
                  <Badge variant="outline">AI {STATES[e.ai_status]}</Badge>
                  <Status value={e.human_status} />
                </div>
                <h2 className="text-sm font-semibold leading-relaxed">
                  {e.assertion}
                </h2>
                <p className="mt-2 text-sm text-muted-foreground">
                  {e.source.title}
                </p>
                <p className="mt-2 text-xs text-muted-foreground">
                  {e.source.locator}
                </p>
              </div>
              <ArrowRight className="mt-2 size-4 shrink-0" />
            </button>
          ))}
        </div>
      ) : (
        !list.loading && (
          <NoData
            title={
              query
                ? "没有找到相关证据"
                : scope === "published"
                  ? "还没有已发布的临床证据"
                  : "这个状态下还没有证据"
            }
            description="正式证据来自已入库的真实资料。配置模型并完成抽取、原文定位校验及 AI 审核后，才会出现在这里。原教学样例保留在教学验证区。"
          />
        )
      )}
      <div className="flex flex-wrap items-center justify-between gap-4 text-sm">
        <a
          href="/evidence/sources/"
          className="text-primary underline underline-offset-4"
        >
          添加或查看文本资料
        </a>
        <a
          href="/tools/teaching/"
          className="text-muted-foreground underline underline-offset-4"
        >
          教学流程验证（虚构资料）
        </a>
      </div>
      <p className="text-sm/relaxed text-muted-foreground">
        “AI
        通过”表示通过该版本的审核流程；不会自动证明临床适用性。使用时仍需核对原文、人群、研究类型和局限。
      </p>
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
      <Sheet
        open={Boolean(selected)}
        onOpenChange={(open) => {
          if (!open) setSelected(null);
        }}
      >
        <SheetContent className="data-[side=right]:w-full overflow-y-auto data-[side=right]:sm:max-w-2xl">
          <SheetHeader>
            <SheetTitle>证据与审核记录</SheetTitle>
            <SheetDescription>
              核对完整语境，抽查结果会记录当前账号与理由。
            </SheetDescription>
          </SheetHeader>
          <div className="flex flex-col gap-6 p-5 text-sm">
            <ErrorMessage error={detail.error || error} />
            {item && (
              <>
                <div className="flex flex-wrap gap-2">
                  <Status value={item.publication_status} />
                  <Badge variant="outline">AI {STATES[item.ai_status]}</Badge>
                  <Status value={item.human_status} />
                </div>
                <h2 className="text-base font-semibold leading-relaxed">
                  {item.assertion}
                </h2>
                <div>
                  <h3 className="mb-2 font-semibold">原文引句</h3>
                  <blockquote className="border-l-2 border-primary pl-4 leading-relaxed">
                    {item.quote}
                  </blockquote>
                  <p className="mt-3 text-xs text-muted-foreground">
                    {item.source.locator} · {item.source.parser_version}
                  </p>
                </div>
                <div>
                  <h3 className="mb-2 font-semibold">适用语境与局限</h3>
                  <dl className="space-y-3">
                    {Object.entries(item.context).map(([key, value]) => (
                      <div key={key}>
                        <dt className="text-muted-foreground">
                          {{
                            population: "人群",
                            study_type: "研究类型",
                            limitations: "局限",
                          }[key] ?? key}
                        </dt>
                        <dd className="mt-1 break-words leading-relaxed">
                          {value == null
                            ? "未知"
                            : typeof value === "string"
                              ? value
                              : JSON.stringify(value)}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </div>
                <div>
                  <h3 className="mb-2 font-semibold">关联标准实体</h3>
                  <div className="flex flex-wrap gap-2">
                    {item.entity_links.length ? (
                      item.entity_links.map((entity) => (
                        <a
                          key={`${entity.namespace}:${entity.identifier}`}
                          className="text-primary underline"
                          href={
                            entity.namespace === "NCBIGene"
                              ? `/knowledge/genes/?q=${entity.identifier}`
                              : `/knowledge/${entity.namespace === "RXNORM" ? "drugs" : "diseases"}/?q=${encodeURIComponent(entity.identifier)}`
                          }
                        >
                          {entity.label} · {entity.identifier}
                        </a>
                      ))
                    ) : (
                      <span className="text-muted-foreground">
                        尚无已确认的标准实体关联
                      </span>
                    )}
                  </div>
                </div>
                <div className="space-y-3 border-t pt-4">
                  <h3 className="font-semibold">资料版本</h3>
                  <a
                    href={`/evidence/sources/?material=${item.source.material_id}`}
                    className="text-primary underline"
                  >
                    {item.source.title}
                  </a>
                  <p className="break-all text-xs text-muted-foreground">
                    SHA-256：{item.source.sha256}
                  </p>
                  <p className="whitespace-pre-wrap break-words leading-relaxed">
                    {item.source.text}
                  </p>
                </div>
                <section className="space-y-4 border-t pt-4">
                  <h3 className="font-semibold">审核历史</h3>
                  {item.reviews.map((r) => (
                    <div key={r.id} className="space-y-2 border-b pb-4">
                      <p>
                        {r.kind === "ai" ? "AI 审核" : `人工抽查 · ${r.actor}`}{" "}
                        · {STATES[r.decision]} · {date(r.created_at)}
                      </p>
                      <p className="leading-relaxed">{r.reason}</p>
                      {Object.entries(r.provenance).map(([key, value]) => (
                        <p
                          key={key}
                          className="break-words text-xs text-muted-foreground"
                        >
                          {key}：{value}
                        </p>
                      ))}
                    </div>
                  ))}
                </section>
                {item.publication_status === "published" && (
                  <section className="space-y-4 border-t pt-4">
                    <h3 className="font-semibold">人工抽查</h3>
                    {session.data?.can_review ? (
                      <>
                        <Field>
                          <FieldLabel htmlFor="review-reason">
                            核验结论或问题说明
                          </FieldLabel>
                          <Textarea
                            id="review-reason"
                            value={reason}
                            onChange={(e) => setReason(e.target.value)}
                            maxLength={10000}
                          />
                        </Field>
                        <div className="flex flex-wrap gap-3">
                          <Button
                            className="h-10"
                            disabled={busy || !reason.trim()}
                            onClick={() => review("confirm")}
                          >
                            确认原文与证据一致
                          </Button>
                          <Button
                            variant="destructive"
                            className="h-10"
                            disabled={busy || !reason.trim()}
                            onClick={() => review("flag")}
                          >
                            发现问题并撤回
                          </Button>
                        </div>
                      </>
                    ) : (
                      <p className="text-muted-foreground">
                        请使用具有证据审核权限的账号登录后记录抽查结果。
                      </p>
                    )}
                  </section>
                )}
              </>
            )}
          </div>
        </SheetContent>
      </Sheet>
      <Sheet
        open={signIn}
        onOpenChange={(open) => {
          if (!busy) {
            setSignIn(open);
            if (!open) setPassword("");
          }
        }}
      >
        <SheetContent className="data-[side=right]:w-full data-[side=right]:sm:max-w-md">
          <SheetHeader>
            <SheetTitle>审阅者登录</SheetTitle>
            <SheetDescription>
              人工确认和撤回会记录到审阅账号。账号由项目管理者配置。
            </SheetDescription>
          </SheetHeader>
          <form onSubmit={authenticate} className="flex flex-col gap-5 p-5">
            <ErrorMessage error={error} />
            <Field>
              <FieldLabel htmlFor="reviewer-name">账号</FieldLabel>
              <Input
                id="reviewer-name"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                required
                className="h-10 text-sm"
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="reviewer-password">密码</FieldLabel>
              <Input
                id="reviewer-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
                className="h-10 text-sm"
              />
            </Field>
            <Button disabled={busy} className="h-10 text-sm">
              {busy ? "登录中…" : "登录"}
            </Button>
          </form>
        </SheetContent>
      </Sheet>
    </AppShell>
  );
}
