export function TranslationLegend() {
  return (
    <details className="text-xs leading-relaxed text-muted-foreground">
      <summary className="w-fit cursor-pointer rounded font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring">中文名称说明</summary>
      <dl className="mt-2 flex flex-col gap-2">
        <div><dt className="inline font-medium text-foreground">参考中文名：</dt><dd className="inline">收录自可追溯的外部来源，不等于已核定的官方中文名。</dd></div>
        <div><dt className="inline font-medium text-foreground">中文草稿：</dt><dd className="inline">本项目补充的译名，保留翻译版本，尚待核对。</dd></div>
        <div><dt className="inline font-medium text-foreground">中文待补充：</dt><dd className="inline">当前尚未收录中文；研发代号等可以保留原文，不强行编造中文药名。</dd></div>
      </dl>
    </details>
  );
}
