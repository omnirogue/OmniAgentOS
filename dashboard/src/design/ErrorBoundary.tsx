"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";
import { ErrorState } from "./ErrorState";

/**
 * A render-error firewall.
 *
 * Without one, a single TypeError inside ONE card unmounts the entire React
 * tree and Next.js replaces the whole page with "Application error: a
 * client-side exception has occurred" — which is exactly how a swarm-run card
 * reading `attempts` as an array (the API returns a map) took /board down with
 * no visible clue as to which card was at fault.
 *
 * Wrap any region whose data comes from an API whose shape can drift. The rest
 * of the page — the toolbar, the filters, the summary — keeps working, and the
 * error message names the region so the failure is diagnosable from the UI.
 */
export interface ErrorBoundaryProps {
  children: ReactNode;
  /** Human name of the region, used in the fallback copy. */
  label?: string;
  /** Rendered instead of the default ErrorState when provided. */
  fallback?: (error: Error, reset: () => void) => ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[ErrorBoundary${this.props.label ? ` ${this.props.label}` : ""}]`, error, info.componentStack);
  }

  reset = () => this.setState({ error: null });

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback(error, this.reset);
    return (
      <ErrorState
        title={this.props.label ? `${this.props.label} failed to render` : "This section failed to render"}
        message={error.message || String(error)}
        onRetry={this.reset}
        retryLabel="Try again"
      />
    );
  }
}
