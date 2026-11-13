import type { Concept } from "@/lib/curation";

export const TRANSLATION_STATUS = {
  confirmed: "中文已确认",
  reference: "参考中文名",
  project_draft: "中文草稿",
};

export function preferredChinese(concept: { translations?: Concept["translations"] }) {
  const chinese = concept.translations?.filter((item) => /^zh(?:-|$)/i.test(item.language) && item.label.trim()) ?? [];
  const preferred = chinese.filter((item) => !/^zh-(?:hant|tw|hk|mo)(?:-|$)/i.test(item.language));
  const translations = preferred.length ? preferred : chinese;
  return translations.find((item) => item.status === "confirmed") ??
    translations.find((item) => item.status === "reference") ??
    translations.find((item) => item.status === "project_draft");
}
