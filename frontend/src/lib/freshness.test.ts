import { describe, expect, it } from "vitest";

import { assessFreshness, STALE_BANNER, UNREACHABLE_BANNER } from "@/lib/freshness";
import type { StatusResponse } from "@/lib/types";

const NOW = new Date("2026-10-02T07:00:00Z");

function status(
  ingestionState: "ok" | "stale",
  dataAsOf: string | null,
  staleAfterMinutes = 5,
): StatusResponse {
  return {
    ingestion_state: ingestionState,
    stale_after_minutes: staleAfterMinutes,
    checked_at: "2026-10-02T07:00:00+00:00",
    feeds: [],
    source: {
      name: "BMKG",
      url: "https://data.bmkg.go.id/",
      notice: "Sumber: BMKG",
      data_as_of: dataAsOf,
    },
  };
}

const minutesAgo = (minutes: number) => new Date(NOW.getTime() - minutes * 60_000).toISOString();

describe("assessFreshness", () => {
  it("fresh data: no banner, and how long ago it was updated", () => {
    const result = assessFreshness(status("ok", minutesAgo(3)), NOW);

    expect(result.stale).toBe(false);
    expect(result.updatedText).toBe("Data terakhir diperbarui 3 menit lalu");
    expect(result.updatedAtWib).toBe("02 Okt 2026 13:57:00 WIB");
  });

  it("the server says stale: banner, even if data_as_of looks recent", () => {
    expect(assessFreshness(status("stale", minutesAgo(2)), NOW).stale).toBe(true);
  });

  it("BMKG never read: banner, and no made-up time", () => {
    const result = assessFreshness(status("ok", null), NOW);

    expect(result.stale).toBe(true);
    expect(result.updatedText).toBe("Belum ada data dari BMKG yang berhasil dibaca.");
    expect(result.updatedAtWib).toBeNull();
  });

  it("data that aged past stale_after_minutes since the status was fetched: banner", () => {
    const result = assessFreshness(status("ok", minutesAgo(12)), NOW);

    expect(result.stale).toBe(true);
    expect(result.updatedText).toBe("Data terakhir diperbarui 12 menit lalu");
  });

  it("right at the threshold is still fresh", () => {
    expect(assessFreshness(status("ok", minutesAgo(5)), NOW).stale).toBe(false);
  });

  it("an unparseable data_as_of is treated as stale", () => {
    expect(assessFreshness(status("ok", "not a date"), NOW).stale).toBe(true);
  });
});

describe("banner wording", () => {
  it("says the data may be outdated and to check BMKG directly", () => {
    expect(STALE_BANNER).toContain("mungkin belum terbaru");
    expect(STALE_BANNER).toContain("bmkg.go.id");
  });

  it("says plainly when the API is unreachable", () => {
    expect(UNREACHABLE_BANNER).toContain("tidak dapat dihubungi");
    expect(UNREACHABLE_BANNER).toContain("bmkg.go.id");
  });

  it("uses no alarming wording", () => {
    for (const text of [STALE_BANNER, UNREACHABLE_BANNER]) {
      expect(text).not.toMatch(/peringatan dini|bahaya|darurat|awas|!/i);
    }
  });
});
