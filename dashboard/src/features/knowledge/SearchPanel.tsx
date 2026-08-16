"use client";

import { useCallback, useState } from "react";
import { Card, EmptyState, Icon, Input, Loading, Pill } from "../../design";
import { searchKnowledge } from "./api";
import type { FactSearchResult } from "./types";

export function SearchPanel() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<FactSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const handleSearch = useCallback(
    async (searchQuery: string) => {
      if (!searchQuery.trim()) {
        setResults([]);
        setSearchError(null);
        return;
      }

      setSearching(true);
      setSearchError(null);
      try {
        const response = await searchKnowledge(searchQuery, 20);
        setResults(response.results);
      } catch (error) {
        const message = error instanceof Error ? error.message : "Search failed";
        setSearchError(message);
        setResults([]);
      } finally {
        setSearching(false);
      }
    },
    [],
  );

  const onQueryChange = async (value: string) => {
    setQuery(value);
    // Debounce search with a small delay
    if (value.trim()) {
      await handleSearch(value);
    } else {
      setResults([]);
      setSearchError(null);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
      <Input
        label="Search knowledge"
        placeholder="Search facts by concept or discipline…"
        value={query}
        onChange={(event) => void onQueryChange(event.target.value)}
        clearable
        onClear={() => {
          setQuery("");
          setResults([]);
          setSearchError(null);
        }}
        icon={<Icon name="search" size={16} />}
      />
      {searching ? <Loading label="Searching facts…" /> : null}
      {searchError ? <EmptyState message={`Search error: ${searchError}`} /> : null}
      {query.trim() && !searching && !searchError && results.length === 0 ? (
        <EmptyState icon={<Icon name="search" size={22} />} message={`No facts match "${query.trim()}".`} />
      ) : null}
      {results.map((result) => (
        <Card key={result.fact.id} padding="sm">
          <strong>{result.fact.statement}</strong>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-2)", marginTop: "var(--space-2)" }}>
            <Pill tone="neutral">{result.fact.discipline}</Pill>
            <Pill tone={result.fact.status === "active" ? "ok" : result.fact.status === "quarantined" ? "warn" : "neutral"}>
              {result.fact.status}
            </Pill>
            <Pill tone="accent">{Math.round(result.fact.trust * 100)}% trust</Pill>
            <Pill tone="accent">Score {(result.score * 100).toFixed(0)}%</Pill>
          </div>
        </Card>
      ))}
    </div>
  );
}
