import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { useCuration } from "@/lib/curation";

const MAIL: Record<string, string> = {
  pending: "邮件等待发送",
  sending: "正在发送邮件",
  sent: "告警邮件已提交至邮件服务器",
  waiting_configuration: "邮件通道待配置",
  failed: "邮件发送失败，请检查邮件通道",
  cancelled: "提醒已解除",
};

export function CollectionAlerts() {
  const { data, error } = useCuration<{
    count: number;
    worker_stalled: boolean;
    results: { id: number; message: string; email_state: string }[];
  }>("collection-alerts/", 15000);
  if (!data && !error) return null;
  if (!error && !data?.count && !data?.worker_stalled) return null;
  return (
    <Alert variant="destructive">
      <AlertTitle>文献采集需要关注</AlertTitle>
      <AlertDescription className="flex flex-col gap-2">
        {error && <p>暂时无法获取采集告警状态，请检查服务连接。</p>}
        {data?.worker_stalled && (
          <p>采集服务心跳已中断超过 90 秒，请检查 worker；当前不能确认下载仍在进行。</p>
        )}
        {data?.results.map((alert) => (
          <p key={alert.id}>
            {alert.message}。{MAIL[alert.email_state] ?? alert.email_state}。
          </p>
        ))}
        <a className="underline" href="/evidence/sources/papers/">查看采集断点与恢复操作</a>
      </AlertDescription>
    </Alert>
  );
}
