export type EvidencePresentation = {
  version: string;
  evidence_level: { status: "unknown" | "recorded"; label: string };
  drug_effect: { status: "unknown" | "recorded"; label: string };
  reported_context: {
    requirements: {
      field: string;
      label: string;
      reported_label: string;
      reported_value: string;
      source: { passage: string; version_id: string; pmid: string; locator: string; start: number; end: number; quote: string };
      choices: { value: string; label: string; status: "consistent" | "different" | "unknown" }[];
    }[];
    unresolved: string[];
    other_conditions_preserved: boolean;
    automatic_patient_eligibility: false;
  };
  informational_retrieval: true;
  treatment_recommendation: false;
};
