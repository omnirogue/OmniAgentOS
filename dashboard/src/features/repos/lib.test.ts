import { describe, expect, it } from "vitest";
import { normalizeGithubOrigin, originMatchesGithubRepo, relativizeHomePath } from "./lib";

describe("GitHub origin normalization", () => {
  it("normalizes SSH and HTTPS GitHub remotes to the same key", () => {
    expect(normalizeGithubOrigin("git@github.com:Globex/Widget.git")).toBe("globex/widget");
    expect(normalizeGithubOrigin("https://github.com/Globex/Widget")).toBe("globex/widget");
    expect(originMatchesGithubRepo("ssh://git@github.com/Globex/Widget.git", "globex", "widget")).toBe(true);
  });

  it("does not treat non-GitHub URLs as GitHub repositories", () => {
    expect(normalizeGithubOrigin("https://gitlab.com/Globex/Widget.git")).toBeNull();
  });
});

describe("home path display", () => {
  it("uses a tilde only for an exact home prefix", () => {
    expect(relativizeHomePath("/Users/owner/Repos/widget", "/Users/owner")).toBe("~/Repos/widget");
    expect(relativizeHomePath("/Users/youruser/Repos/widget", "/Users/owner")).toBe("/Users/youruser/Repos/widget");
  });
});
