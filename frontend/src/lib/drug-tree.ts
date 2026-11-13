import type { Concept, Release } from "@/lib/curation";

export type DrugClass = {
  identifier: string;
  label: string;
  translations: Concept["translations"];
  child_count: number;
  source_member_count: number;
  unavailable_count: number;
  matching_child_count: number;
};

export type DrugTreePage = {
  state: "ready" | "waiting_dataset";
  release: Release;
  dataset: {
    id: string;
    namespace: string;
    version: string;
    source_url: string;
    imported_at: string;
  } | null;
  kind: "classes" | "ingredients";
  class: DrugClass | null;
  offset: number;
  limit: number;
  total: number;
  results: DrugClass[] | Concept[];
  coverage?: { classes: number; classified_ingredients: number; local_ingredients: number };
  notice: string;
};
