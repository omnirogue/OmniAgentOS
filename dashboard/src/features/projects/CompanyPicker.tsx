"use client";

import { useEffect, useState } from "react";
import { Select, useToast } from "@/design";
import { orgdimsApi } from "@/features/orgdims/api";
import { patchProject, ProjectApiError } from "./api";
import type { Project } from "./types";

/** The `Unassigned` sentinel — never a real company slug. */
const UNASSIGNED = "";

type CompanyOption = { id: string; slug: string; name: string };

export type CompanyPickerProps = {
  project: Project;
  /** Called with the server's own PATCH response after a successful save. */
  onUpdated: (project: Project) => void;
};

/**
 * A data-driven company assignment control for one project — GET
 * /api/orgdims/companies feeds the option list (never hardcoded), and picking
 * one PATCHes /api/projects/{id} with the slug (server resolves it to the
 * canonical org_companies id). "Unassigned" clears the assignment.
 */
export function CompanyPicker({ project, onUpdated }: CompanyPickerProps) {
  const [companies, setCompanies] = useState<CompanyOption[]>([]);
  const [saving, setSaving] = useState(false);
  const { push } = useToast();

  useEffect(() => {
    let cancelled = false;
    orgdimsApi
      .companies()
      .then((response) => {
        if (cancelled) return;
        const rows = Array.isArray(response.companies) ? response.companies : [];
        setCompanies(
          rows.map((row) => ({
            id: String(row.id),
            slug: String(row.slug),
            name: String(row.name ?? row.slug),
          })),
        );
      })
      .catch(() => {
        if (!cancelled) setCompanies([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // The wire value is the company id; the picker (and the PATCH body) speaks
  // in slugs, so map id -> slug for the controlled value.
  const selectedSlug = companies.find((c) => c.id === project.org_company_id)?.slug ?? UNASSIGNED;

  const options = [
    { value: UNASSIGNED, label: "Unassigned" },
    ...companies.map((c) => ({ value: c.slug, label: c.name })),
  ];

  const handleChange = async (slug: string) => {
    if (slug === selectedSlug || saving) return;
    setSaving(true);
    try {
      const updated = await patchProject(project.id, {
        org_company_id: slug === UNASSIGNED ? null : slug,
      });
      onUpdated(updated);
      const label = options.find((o) => o.value === slug)?.label ?? slug;
      push({ title: "Company updated", message: `${project.name} → ${label}.`, tone: "success" });
    } catch (reason) {
      const message =
        reason instanceof ProjectApiError || reason instanceof Error
          ? reason.message
          : "Could not update the project's company.";
      push({ title: "Company update failed", message, tone: "error" });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Select
      label="Company"
      options={options}
      value={selectedSlug}
      onChange={(value) => void handleChange(value)}
      disabled={saving}
      placeholder="Unassigned"
      aria-label={`Company for ${project.name}`}
    />
  );
}
