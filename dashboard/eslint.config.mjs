import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({ baseDirectory: import.meta.dirname });

// Inline-style policy (design-system migration, FINAL-PLAN.md §8):
//
//   * Static layout in `style={{}}` is BANNED in feature/app code — it goes in
//     the feature's CSS Module using the theme tokens (see theme.css). The
//     design primitives themselves are exempt (src/design is not matched by
//     the globs below).
//   * Dynamic values are piped through CSS custom properties instead:
//     `style={{ "--pct": `${pct}%` }}` with the module class consuming
//     `var(--pct, 0)` (established pattern: collab.module.css `.column` +
//     `--column-tone`). The selector therefore bans any inline property whose
//     key is NOT a `--*` custom property (identifier keys like `width`, string
//     keys like `"margin"`, and spread of style objects).
//   * LEGACY RATCHET: the v0 surfaces not yet migrated to design primitives
//     are grandfathered below, file by file, so the ban has full force on all
//     new and migrated code while the legacy tail is migrated incrementally.
//     When you migrate a file to CSS Modules, DELETE its entry from
//     `legacyInlineStyleAllowlist` — never add new entries.
const legacyInlineStyleAllowlist = [
  "src/app/accounts/page.tsx",
  "src/app/agents/[[]id]/page.tsx",
  "src/app/agents/new/page.tsx",
  "src/app/approvals/page.tsx",
  "src/app/briefing/page.tsx",
  "src/app/capabilities/page.tsx",
  "src/app/design/page.tsx",
  "src/app/files/page.tsx",
  "src/app/goals/[[]id]/page.tsx",
  "src/app/knowledge/page.tsx",
  "src/app/projects/[[]id]/page.tsx",
  "src/app/projects/overview/page.tsx",
  "src/app/projects/page.tsx",
  "src/app/projects/task/[[]id]/page.tsx",
  "src/app/reliability/page.tsx",
  "src/app/runs/[[]id]/page.tsx",
  "src/app/skills/layout.tsx",
  "src/app/skills/page.tsx",
  "src/app/vault/layout.tsx",
  "src/app/vault/page.tsx",
  "src/app/vault/search/page.tsx",
  "src/features/accounts/UsageMeters.tsx",
  "src/features/artifacts/ArtifactPreview.tsx",
  "src/features/board/BoardProgress.tsx",
  "src/features/capabilities/CapabilityPicker.tsx",
  "src/features/capabilities/ServersSection.tsx",
  "src/features/cash/components/DataQualityPanel.tsx",
  "src/features/chats/SessionFollow.tsx",
  "src/features/chats/TerminalView.tsx",
  "src/features/cockpit/TaskPulse.tsx",
  "src/features/experiments/ExperimentCockpit.tsx",
  "src/features/filesearch/components/FileSearchPanel.tsx",
  "src/features/galaxy/Backlinks.tsx",
  "src/features/galaxy/GalaxyGraph.tsx",
  "src/features/galaxy/GalaxyLegend.tsx",
  "src/features/galaxy/GalaxyPanel.tsx",
  "src/features/galaxy/NoteReader.tsx",
  "src/features/galaxy/SearchPanel.tsx",
  "src/features/galaxy/markdown.tsx",
  "src/features/knowledge/HealthChip.tsx",
  "src/features/knowledge/KnowledgeGraph.tsx",
  "src/features/knowledge/PromotedFeed.tsx",
  "src/features/knowledge/RecallReplay.tsx",
  "src/features/knowledge/SearchPanel.tsx",
  "src/features/memory/ArtifactsList.tsx",
  "src/features/memory/MemoryManager.tsx",
  "src/features/mounts/components/MountBrowser.tsx",
  "src/features/mounts/components/MountGrid.tsx",
  "src/features/orgdims/MatrixView.tsx",
  "src/features/orgdims/PortfolioView.tsx",
  "src/features/preflight/PreflightCard.tsx",
  "src/features/projects/HierarchyViews.tsx",
  "src/features/revenue/components/DataQualityPanel.tsx",
  "src/features/skills/Breadcrumbs.tsx",
  "src/features/skills/ExecutionsList.tsx",
  "src/features/skills/ProposalCard.tsx",
  "src/features/skills/SkillDetail.tsx",
  "src/features/skills/SkillEditDialog.tsx",
  "src/features/skills/SkillSearchBox.tsx",
  "src/features/skills/SkillTree.tsx",
  "src/features/skills/SkillsGraphView.tsx",
  "src/features/skills/UpdatesQueue.tsx",
  "src/features/steward/components/BriefingCard.tsx",
  "src/features/steward/components/CommsList.tsx",
  "src/features/steward/components/SuggestionCard.tsx",
  "src/features/swarm/FleetStrip.tsx",
  "src/features/swarm/SwarmRunCard.tsx",
  "src/features/swarm/SwarmRunModal.tsx",
];

const inlineStyleSelectors = [
  {
    selector: "JSXAttribute[name.name='style']:has(Property[key.type='Identifier'])",
    message:
      "Inline `style={{}}` is banned in feature/app code. Use CSS Modules with theme tokens (see theme.css). Dynamic values go through custom properties: style={{ '--name': value }} + CSS var(--name). Design primitives in src/design/ are exempt.",
  },
  {
    selector: "JSXAttribute[name.name='style']:has(Property[key.type='Literal'][key.value!=/^--/])",
    message:
      "Inline `style={{}}` is banned in feature/app code. Use CSS Modules with theme tokens (see theme.css). Dynamic values go through custom properties: style={{ '--name': value }} + CSS var(--name). Design primitives in src/design/ are exempt.",
  },
  {
    selector: "JSXAttribute[name.name='style']:has(SpreadElement)",
    message:
      "Inline `style={{}}` is banned in feature/app code. Use CSS Modules with theme tokens (see theme.css). Dynamic values go through custom properties: style={{ '--name': value }} + CSS var(--name). Design primitives in src/design/ are exempt.",
  },
];

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    files: ["src/features/**/*.tsx", "src/features/**/*.ts", "src/app/**/*.tsx", "src/app/**/*.ts"],
    ignores: legacyInlineStyleAllowlist,
    rules: {
      "no-restricted-syntax": ["error", ...inlineStyleSelectors],
    },
  },
];

export default eslintConfig;
