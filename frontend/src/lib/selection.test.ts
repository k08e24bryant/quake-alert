import { describe, expect, it } from "vitest";

import { select } from "@/lib/selection";

describe("select", () => {
  it("makes every click a new selection, even of the same quake", () => {
    const first = select(null, "a");
    const again = select(first, "a");

    expect(again.id).toBe("a");
    expect(again.seq).not.toBe(first.seq);
    expect(again).not.toBe(first);
  });

  it("switches quakes", () => {
    expect(select(select(null, "a"), "b")).toEqual({ id: "b", seq: 2 });
  });
});
