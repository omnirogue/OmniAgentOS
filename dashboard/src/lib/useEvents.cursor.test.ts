import { beforeEach, describe, expect, it } from "vitest";
import { eventCursorKey, readStoredLastId, writeStoredLastId } from "./useEvents";

/**
 * The SSE cursor is stored PER event-type filter. Sharing one cursor across
 * differently-filtered connections is not a nicety: a cursor advanced by a
 * board stream would make a revenue stream reconnect past revenue events it
 * never received, and those rows are gone — the client's only recovery is a
 * full refetch it does not know it needs.
 */
describe("event cursor storage", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it("keys the cursor by the types filter", () => {
    expect(eventCursorKey("board.updated")).not.toBe(eventCursorKey("revenue.updated"));
  });

  it("round-trips a cursor per filter without cross-talk", () => {
    const board = eventCursorKey("board.updated");
    const revenue = eventCursorKey("revenue.updated");
    writeStoredLastId(900, board);
    expect(readStoredLastId(board)).toBe(900);
    expect(readStoredLastId(revenue)).toBe(0);
  });

  it("reads 0 for an unseen filter rather than throwing", () => {
    expect(readStoredLastId(eventCursorKey("never.seen"))).toBe(0);
  });

  it("ignores a corrupted stored value instead of sending NaN upstream", () => {
    const key = eventCursorKey("board.updated");
    sessionStorage.setItem(key, "not-a-number");
    expect(readStoredLastId(key)).toBe(0);
  });

  it("ignores a negative stored value", () => {
    const key = eventCursorKey("board.updated");
    sessionStorage.setItem(key, "-5");
    expect(readStoredLastId(key)).toBe(0);
  });
});
