import type { Role } from "./api/types";

export interface NavItem {
  path: string;
  label: string;
  adminOnly: boolean;
}

export const NAV: NavItem[] = [
  { path: "/review", label: "Review queue", adminOnly: false },
  { path: "/ontology", label: "Ontology", adminOnly: false },
  { path: "/proposals", label: "Proposals", adminOnly: false },
  { path: "/mappings", label: "Mappings", adminOnly: true },
  { path: "/scoring", label: "Scoring", adminOnly: false },
  { path: "/runs", label: "Runs", adminOnly: false },
  { path: "/audit", label: "Audit", adminOnly: true },
  { path: "/users", label: "Users", adminOnly: true },
];

export function navFor(role: string): NavItem[] {
  return NAV.filter((item) => !item.adminOnly || role === "admin");
}

export const isAdmin = (role: string | undefined): role is Role => role === "admin";
