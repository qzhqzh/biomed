import { useId, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { EvidencePresentation } from "@/lib/knowledge-presentation";

export function EvidenceLabels({ data }: { data?: EvidencePresentation }) {
  if (!data) return null;
  return <div className="flex flex-wrap gap-2" aria-label="证据等级与药效状态">
    <Badge variant="outline">证据等级：{data.evidence_level.label}</Badge>
    <Badge variant="outline">药效：{data.drug_effect.label}</Badge>
  </div>;
}

export function EvidenceContext({ data }: { data?: EvidencePresentation }) {
  const prefix = useId();
  const [selected, setSelected] = useState<Record<string, string>>({});
  if (!data) return null;
  const { requirements, unresolved, other_conditions_preserved: otherConditions } = data.reported_context;
  return <section aria-label="文献研究范围对照" className="flex min-w-0 flex-col gap-3">
    <h3 className="font-medium">文献研究范围对照</h3>
    <p className="text-xs/relaxed text-muted-foreground">仅对照文献记录的条件，不判断患者是否适用或推荐治疗。选择只保存在当前页面，请勿输入患者资料。</p>
    <p className="text-xs/relaxed text-muted-foreground">条件依据抽取字段和同版原文定位展示，语义是否准确仍以知识的独立核验状态为准。</p>
    <EvidenceLabels data={data} />
    <p className="text-xs/relaxed text-muted-foreground">未记录等级或药效时保持未知；不会把部分缓解或生存期自动判为敏感、耐药或证据分级。</p>
    {data.evidence_level.status === "unknown" || data.drug_effect.status === "unknown" ? <p className="text-xs/relaxed text-muted-foreground">目标系统如要求等级或药效，未知值不能自动补填；仍须核对其必填约束。</p> : null}
    {requirements.length ? <FieldGroup className="gap-4">{requirements.map((requirement) => {
      const choice = requirement.choices.find((item) => item.value === selected[requirement.field]);
      const result = choice?.status === "consistent" ? "与文献记录的范围一致"
        : choice?.status === "different" ? "与文献记录的范围不同，知识仍可查看"
          : "未提供或无法识别，暂不能比较";
      const id = `${prefix}-${requirement.field}`;
      return <Field key={requirement.field}>
        <FieldLabel htmlFor={id}>{requirement.label} · 文献：{requirement.reported_label}</FieldLabel>
        <Select value={selected[requirement.field] ?? "not_provided"}
          onValueChange={(value) => setSelected((current) => ({ ...current, [requirement.field]: value }))}>
          <SelectTrigger id={id} className="max-w-sm"><SelectValue /></SelectTrigger>
          <SelectContent><SelectGroup>
            <SelectItem value="not_provided">未提供／不确定</SelectItem>
            {requirement.choices.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}
          </SelectGroup></SelectContent>
        </Select>
        <p role="status" className="text-sm/relaxed">{result}</p>
        <p className="break-words text-xs/relaxed text-muted-foreground">原文：<span lang="en">{requirement.source.quote}</span> · {requirement.source.locator} · 字符 [{requirement.source.start}, {requirement.source.end})</p>
      </Field>;
    })}</FieldGroup> : <p className="text-sm/relaxed text-muted-foreground">暂无可自动对照的明确研究范围；不能据此推断文献没有条件。</p>}
    {unresolved.map((reason) => <p key={reason} className="text-xs/relaxed text-muted-foreground">{reason}</p>)}
    {otherConditions ? <p className="text-xs/relaxed text-muted-foreground">其余条件保留在原始字段和原文中，尚未自动对照。</p> : null}
  </section>;
}
