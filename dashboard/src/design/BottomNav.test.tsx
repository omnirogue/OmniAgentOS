import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { BOTTOM_NAV_ITEMS, BottomNav, bottomNavActiveKey } from "./BottomNav";

describe("bottomNavActiveKey", () => {
  it("lights the tab that owns the route", () => {
    expect(bottomNavActiveKey("/")).toBe("status");
    expect(bottomNavActiveKey("/board")).toBe("board");
    expect(bottomNavActiveKey("/inbox")).toBe("inbox");
    expect(bottomNavActiveKey("/sessions")).toBe("sessions");
  });

  it("keeps the tab lit on nested detail routes", () => {
    expect(bottomNavActiveKey("/board/task/42")).toBe("board");
    expect(bottomNavActiveKey("/inbox/pending")).toBe("inbox");
    expect(bottomNavActiveKey("/sessions/abc123")).toBe("sessions");
  });

  it("does not treat a shared prefix as a match", () => {
    // /boardgames is not /board, /sessionsplayer is not /sessions.
    expect(bottomNavActiveKey("/boardgames")).toBeNull();
    expect(bottomNavActiveKey("/sessionsplayer")).toBeNull();
  });

  it("resolves aliased routes to the tab that owns them", () => {
    expect(bottomNavActiveKey("/activity")).toBe("board");
    expect(bottomNavActiveKey("/approvals")).toBe("inbox");
    expect(bottomNavActiveKey("/alerts")).toBe("inbox");
    expect(bottomNavActiveKey("/briefing")).toBe("inbox");
  });

  it("prefers the most specific match — /approvals is not shadowed by /inbox", () => {
    expect(bottomNavActiveKey("/approvals")).toBe("inbox");
    expect(bottomNavActiveKey("/approvals/pending")).toBe("inbox");
  });

  it("lights nothing for routes outside the four tabs", () => {
    expect(bottomNavActiveKey("/skills")).toBeNull();
    expect(bottomNavActiveKey("/companies")).toBeNull();
    expect(bottomNavActiveKey(null)).toBeNull();
    expect(bottomNavActiveKey(undefined)).toBeNull();
  });

  it("ignores a trailing slash", () => {
    expect(bottomNavActiveKey("/board/")).toBe("board");
    expect(bottomNavActiveKey("/")).toBe("status");
  });
});

describe("<BottomNav/>", () => {
  it("renders the four tabs plus More", () => {
    render(<BottomNav pathname="/" onMore={() => {}} />);
    const nav = screen.getByRole("navigation", { name: /primary \(compact\)/i });
    expect(nav).toBeTruthy();
    for (const item of BOTTOM_NAV_ITEMS) {
      expect(screen.getByRole("link", { name: new RegExp(item.label, "i") })).toBeTruthy();
    }
    expect(screen.getByRole("button", { name: /more/i })).toBeTruthy();
  });

  it("marks exactly one tab as the current page", () => {
    render(<BottomNav pathname="/board/task/7" onMore={() => {}} />);
    const current = screen.getAllByRole("link").filter((el) => el.getAttribute("aria-current") === "page");
    expect(current).toHaveLength(1);
    expect(current[0]?.textContent).toContain("Board");
  });

  it("marks no tab current on a route outside the tabs", () => {
    render(<BottomNav pathname="/skills" onMore={() => {}} />);
    expect(screen.queryAllByRole("link").filter((el) => el.getAttribute("aria-current") === "page")).toHaveLength(0);
  });

  it("opens the drawer from More and reports its state", async () => {
    const onMore = vi.fn();
    const { rerender } = render(<BottomNav pathname="/" onMore={onMore} moreOpen={false} />);
    const more = screen.getByRole("button", { name: /more/i });
    expect(more.getAttribute("aria-expanded")).toBe("false");
    await userEvent.click(more);
    expect(onMore).toHaveBeenCalledTimes(1);
    rerender(<BottomNav pathname="/" onMore={onMore} moreOpen />);
    expect(screen.getByRole("button", { name: /more/i }).getAttribute("aria-expanded")).toBe("true");
  });

  it("shows a count badge only when there is something waiting", () => {
    const { rerender } = render(<BottomNav pathname="/" onMore={() => {}} counts={{ inbox: 3 }} />);
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("3 waiting")).toBeTruthy();
    rerender(<BottomNav pathname="/" onMore={() => {}} counts={{ inbox: 0 }} />);
    expect(screen.queryByText("0")).toBeNull();
  });

  it("clamps a large count to 99+", () => {
    render(<BottomNav pathname="/" onMore={() => {}} counts={{ inbox: 250 }} />);
    expect(screen.getByText("99+")).toBeTruthy();
    expect(screen.getByText("250 waiting")).toBeTruthy();
  });
});
