import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Empty,
  EmptyHeader,
  EmptyTitle,
  EmptyDescription,
} from "@/components/ui/empty";
import { AlertCircle } from "lucide-react";
import { STATES } from "@/lib/curation";

export function ErrorMessage({ error }: { error: string }) {
  return error ? (
    <Alert variant="destructive">
      <AlertCircle />
      <AlertDescription>{error}</AlertDescription>
    </Alert>
  ) : null;
}
export function Status({ value }: { value: string }) {
  return (
    <Badge
      variant={
        value === "failed" || value === "flagged" ? "destructive" : "secondary"
      }
    >
      {STATES[value] ?? value}
    </Badge>
  );
}
export function NoData({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <Empty className="border py-12">
      <EmptyHeader>
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription>{description}</EmptyDescription>
      </EmptyHeader>
    </Empty>
  );
}
