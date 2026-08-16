"use client";

import { Select } from "../../design";
import { useProjectContext } from "./context";

/** Compact global project selector. Renders nothing until at least one project
 * exists so the top bar stays clean on a fresh install. */
export function ProjectSwitcher() {
  const { projects, activeProjectId, setActiveProjectId, loading } = useProjectContext();
  if (!loading && projects.length === 0) return null;
  return (
    <Select
      label="Project"
      value={activeProjectId}
      onChange={setActiveProjectId}
      disabled={loading}
      options={[
        { value: "", label: "All projects" },
        ...projects.map((p) => ({ value: p.id, label: p.name })),
      ]}
    />
  );
}
