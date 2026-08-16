"use client";

import { useEffect, useState } from "react";

/** Read a CSS custom property from :root / documentElement (theme-aware). */
export function readCssVar(name: string, fallback = ""): string {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

/** Subscribe to theme changes (data-theme attr) and re-read vars. */
export function useCssVars(names: string[]): Record<string, string> {
  const [vars, setVars] = useState<Record<string, string>>(() =>
    Object.fromEntries(names.map((n) => [n, ""])),
  );

  useEffect(() => {
    const read = () => {
      const next: Record<string, string> = {};
      for (const n of names) next[n] = readCssVar(n);
      setVars(next);
    };
    read();
    const obs = new MutationObserver(read);
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme", "class", "style"],
    });
    return () => obs.disconnect();
    // names is expected to be a stable constant array from the caller
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [names.join("|")]);

  return vars;
}

export const CHART_THEME_VARS = [
  "--text",
  "--text-muted",
  "--text-faint",
  "--border",
  "--border-strong",
  "--surface",
  "--overlay",
  "--accent",
  "--promote",
  "--reject",
  "--canvas",
] as const;
