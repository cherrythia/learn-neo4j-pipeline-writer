// Friendly names for the generated schema types.
import type { components } from "./schema";

type S = components["schemas"];
export type Me = S["Me"];
export type Role = "admin" | "reviewer";
export type ReviewItem = S["ReviewOut"];
export type VersionOut = S["VersionOut"];
export type VersionDetail = S["VersionDetail"];
export type ClassIn = S["ClassIn"];
export type RelationIn = S["RelationIn"];
export type PropertyIn = S["PropertyIn"];
export type ProposalOut = S["ProposalOut"];
export type MappingOut = S["MappingOut"];
export type ScoringPolicy = S["ScoringPolicy"];
export type RunOut = S["writer_service__api__admin__RunOut"];
export type RunDetail = S["RunDetail"];
export type AuditOut = S["AuditOut"];
export type UserOut = S["UserOut"];

/** The ontology spec as the API returns it inside VersionDetail (see ontology/spec.py). */
export interface PropertySpec {
  name: string;
  datatype: string;
  required: boolean;
  description?: string | null;
}
export interface ClassSpec {
  name: string;
  identity_key: string;
  properties: PropertySpec[];
  parent: string | null;
  namespace: string;
  description?: string | null;
  deprecated: boolean;
}
export interface RelationSpec {
  name: string;
  domain: string;
  range: string;
  cardinality: string;
  namespace: string;
  description?: string | null;
  deprecated: boolean;
}
export interface OntologySpec {
  classes: ClassSpec[];
  relations: RelationSpec[];
}

export function specOf(v: VersionDetail): OntologySpec {
  return {
    classes: v.classes as unknown as ClassSpec[],
    relations: v.relations as unknown as RelationSpec[],
  };
}

/** Score derivation stored with each candidate (scoring/engine.py Score.as_dict + routing). */
export interface Breakdown {
  value?: number;
  components?: Record<string, { value: number; weight: number; contribution: number }>;
  decision?: string;
  reasons?: string[];
  notes?: string[];
}

export interface EvidenceItem {
  source_type: string;
  source_ref: string;
  locator: string;
  quote: string;
}
