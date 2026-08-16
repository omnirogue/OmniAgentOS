/** Vault Browser / Memory Galaxy — feature module public surface. */

export * from "./types";
export * from "./paths";
export { noteTypeColor, noteTypeLabel, noteTypeOrder, statusTone, confidenceTone } from "./noteType";
export { buildLinkIndex, resolveWikilink, backlinksFor, type VaultLinkIndex } from "./linkResolver";
export { buildGraph, buildGraphData, computeLayout, type GraphNode, type GraphEdge, type GraphData } from "./layout";
export {
  fetchVaultTree,
  fetchVaultNote,
  searchVault,
  normalizeTree,
  normalizeNoteDetail,
  normalizeSearchHits,
  VaultApiError,
} from "./api";
export { FIXTURE_TREE, FIXTURE_DETAILS, fixtureSearch } from "./fixtures";
export { MarkdownBody, stripLeadingH1, type WikilinkResolver } from "./markdown";
export { useVaultData, USE_FIXTURES, type VaultDataState } from "./useVaultData";
export { useNoteDetail, type NoteDetailState } from "./useNoteDetail";
export { VaultDataProvider, useVaultDataContext } from "./VaultDataContext";
export { GalaxyGraph, type GalaxyGraphProps } from "./GalaxyGraph";
export { GalaxyLegend, type GalaxyLegendProps } from "./GalaxyLegend";
export { GalaxyPanel } from "./GalaxyPanel";
export { NoteReader, type NoteReaderProps } from "./NoteReader";
export { Backlinks, type BacklinksProps } from "./Backlinks";
export { SearchPanel, type SearchPanelProps } from "./SearchPanel";
export { vaultCommandItems, useVaultCommands } from "./commands";
