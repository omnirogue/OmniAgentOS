import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import SessionsError from "../sessions/error";
import BoardError from "../board/error";
import ApprovalsError from "../inbox/error";
import GlobalError from "../global-error";
import errorBoundaryStyles from "../errorBoundary.module.css";

/**
 * Test suite for route-segment error boundaries.
 * Verifies that each error.tsx component properly displays errors,
 * provides reset functionality, and includes error digest for debugging.
 */

/**
 * jsdom never sees the app's CSS: vitest runs with `css: false`, so importing
 * a `*.module.css` yields a class-name proxy and no stylesheet is attached to
 * the test document — every computed style comes back empty and any
 * `toHaveStyle` assertion fails regardless of what the component does.
 *
 * Load the real stylesheet from disk and rewrite its class selectors to the
 * (hashed) names the proxy hands the components, so the style assertions below
 * run against the CSS that actually ships. Delete `font-family` from
 * `.digestText` in errorBoundary.module.css and these tests go red — which is
 * the whole point of asserting on style rather than on "has some className".
 */
function installErrorBoundaryStylesheet(): () => void {
  const cssPath = path.join(
    path.dirname(fileURLToPath(import.meta.url)),
    "..",
    "errorBoundary.module.css"
  );
  const classNames = errorBoundaryStyles as unknown as Record<string, string>;
  const scopedCss = readFileSync(cssPath, "utf8").replace(
    /\.(-?[A-Za-z_][\w-]*)/g,
    (_match, className: string) => `.${classNames[className] ?? className}`
  );
  const styleEl = document.createElement("style");
  styleEl.textContent = scopedCss;
  document.head.appendChild(styleEl);
  return () => styleEl.remove();
}

describe("Route Error Boundaries", () => {
  let removeErrorBoundaryStylesheet: () => void;

  beforeAll(() => {
    removeErrorBoundaryStylesheet = installErrorBoundaryStylesheet();
  });

  afterAll(() => {
    removeErrorBoundaryStylesheet();
  });

  describe("SessionsError", () => {
    it("renders error state with sessions-specific title", () => {
      const error = new Error("Session fetch failed");
      const reset = vi.fn();

      render(<SessionsError error={error} reset={reset} />);

      expect(screen.getByText("Sessions view failed to load")).toBeInTheDocument();
      expect(screen.getByText(/Unable to display your sessions/)).toBeInTheDocument();
    });

    it("provides a reload sessions button", async () => {
      const error = new Error("Session fetch failed");
      const reset = vi.fn();
      render(<SessionsError error={error} reset={reset} />);

      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: "Reload sessions" }));

      expect(reset).toHaveBeenCalledOnce();
    });
  });

  describe("BoardError", () => {
    it("renders error state with board-specific title", () => {
      const error = new Error("Board data shape mismatch");
      const reset = vi.fn();

      render(<BoardError error={error} reset={reset} />);

      expect(screen.getByText("Board view failed to load")).toBeInTheDocument();
      expect(screen.getByText(/Unable to display the kanban board/)).toBeInTheDocument();
    });

    it("provides a reload board button", async () => {
      const error = new Error("Board data shape mismatch");
      const reset = vi.fn();
      render(<BoardError error={error} reset={reset} />);

      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: "Reload board" }));

      expect(reset).toHaveBeenCalledOnce();
    });
  });

  describe("ApprovalsError", () => {
    it("renders error state with approvals-specific title", () => {
      const error = new Error("Approvals queue load failed");
      const reset = vi.fn();

      render(<ApprovalsError error={error} reset={reset} />);

      expect(screen.getByText("Approvals view failed to load")).toBeInTheDocument();
      expect(screen.getByText(/Unable to display the approval queue/)).toBeInTheDocument();
    });

    it("provides a reload approvals button", async () => {
      const error = new Error("Approvals queue load failed");
      const reset = vi.fn();
      render(<ApprovalsError error={error} reset={reset} />);

      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: "Reload approvals" }));

      expect(reset).toHaveBeenCalledOnce();
    });
  });

  describe("GlobalError", () => {
    it("renders full-page error with html/body elements", () => {
      const error = new Error("Global error");
      const reset = vi.fn();

      // global-error.tsx REPLACES the root layout when it fires, so it is the
      // only component besides layout.tsx that may render <html>/<body> — and
      // it must, or the app emits invalid markup at the exact moment it is
      // already failing.
      //
      // This cannot be asserted through RTL's `render`: React 19 treats html,
      // head and body as document singletons and drops them when the mount
      // point is a detached <div>, so `container.querySelector("html")` is
      // null no matter what the component returns. DOMParser is no better —
      // it synthesises <html>/<body> around any fragment, so a parsed-document
      // query would still pass after the wrapper was deleted. Server-rendering
      // and asserting on the emitted markup is the one check that actually
      // goes red when the wrapper goes away.
      const markup = renderToStaticMarkup(
        <GlobalError error={error} reset={reset} />
      );

      expect(markup).toMatch(/^<html[^>]*>/);
      expect(markup).toMatch(/<body[^>]*>/);
      expect(markup).toMatch(/<\/body><\/html>$/);
      expect(markup).toContain("Something went wrong");

      // The interactive tree still mounts through RTL (React drops the
      // singletons there, leaving the inner content queryable).
      const { container } = render(<GlobalError error={error} reset={reset} />);

      expect(screen.getByText("Something went wrong")).toBeInTheDocument();
      expect(container.querySelector(".ds-state")).toBeInTheDocument();
    });

    it("displays generic error title for application-wide failures", () => {
      const error = new Error("Global render error");
      const reset = vi.fn();

      render(<GlobalError error={error} reset={reset} />);

      expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    });

    it("provides a try again button", async () => {
      const error = new Error("Global render error");
      const reset = vi.fn();
      render(<GlobalError error={error} reset={reset} />);

      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: "Try again" }));

      expect(reset).toHaveBeenCalledOnce();
    });

    it("displays error digest in monospace font when provided", () => {
      const error = new Error("Global error") as Error & { digest?: string };
      error.digest = "xyz789abc123";
      const reset = vi.fn();

      const { container } = render(<GlobalError error={error} reset={reset} />);

      // Select the digest paragraph by its text: `container.querySelector("p")`
      // returns ErrorState's message paragraph, which is never monospaced.
      const paragraphs = container.querySelectorAll("p");
      const digestText = Array.from(paragraphs).find((p) =>
        p.textContent?.includes("Error ID")
      );
      expect(digestText).toBeInTheDocument();
      expect(digestText).toHaveStyle({ fontFamily: "var(--font-mono)" });
      expect(screen.getByText(/Error ID: xyz789abc123/)).toBeInTheDocument();
    });

    it("logs error to console in development mode", () => {
      const error = new Error("Global error");
      const reset = vi.fn();
      const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {});

      // Temporarily set NODE_ENV to development (vi.stubEnv — direct
      // assignment is a TS2540: NODE_ENV is typed read-only)
      vi.stubEnv("NODE_ENV", "development");

      try {
        render(<GlobalError error={error} reset={reset} />);
        expect(consoleSpy).toHaveBeenCalledWith("Global error:", error);
      } finally {
        vi.unstubAllEnvs();
        consoleSpy.mockRestore();
      }
    });
  });

  describe("Error Boundary Accessibility", () => {
    it("marks error state region with alert role", () => {
      const error = new Error("Test error");
      const reset = vi.fn();

      const { container } = render(<BoardError error={error} reset={reset} />);

      const alertRole = container.querySelector('[role="alert"]');
      expect(alertRole).toBeInTheDocument();
    });

    it("displays error digest in monospace for readability", () => {
      const error = new Error("Test error") as Error & { digest?: string };
      error.digest = "digest123";
      const reset = vi.fn();

      const { container } = render(<BoardError error={error} reset={reset} />);

      // Select the digest paragraph by its text: `container.querySelector("p")`
      // returns ErrorState's message paragraph, which is never monospaced.
      const paragraphs = container.querySelectorAll("p");
      const digestText = Array.from(paragraphs).find((p) =>
        p.textContent?.includes("Error ID")
      );
      expect(digestText).toBeTruthy();
      expect(digestText).toHaveStyle({ fontFamily: "var(--font-mono)" });
    });
  });

  describe("No Stack Trace Leakage", () => {
    it("does not display error.message directly in route errors", () => {
      const error = new Error("Failed due to null reference in getUserData.ts line 42");
      const reset = vi.fn();

      const { container } = render(<BoardError error={error} reset={reset} />);

      // The component should use a generic message, not the raw error message
      expect(container.textContent).not.toContain("line 42");
    });

    it("global error does not leak sensitive error details", () => {
      const error = new Error("Database connection failed: server=prod.internal.corp");
      const reset = vi.fn();

      render(<GlobalError error={error} reset={reset} />);

      // Should use generic message, not the raw error.
      //
      // The shipped copy is the full sentence, so RTL's exact-text query for
      // the bare fragment "An unexpected error occurred" can never match:
      // getByText compares an element's *whole* normalised text content. The
      // honest equivalent is the complete generic message — a stricter match
      // than the fragment, not a looser one — plus an explicit check that no
      // part of the raw error reached the DOM, which is what this describe
      // block is actually about.
      expect(
        screen.getByText(
          "An unexpected error occurred. Please try again or contact support if the problem persists."
        )
      ).toBeInTheDocument();
      expect(document.body.textContent).not.toContain("prod.internal.corp");
      expect(document.body.textContent).not.toContain(
        "Database connection failed"
      );
    });
  });
});
