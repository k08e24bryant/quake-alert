import { readFileSync } from "node:fs";

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Footer } from "@/components/Footer";
import { DISCLAIMER } from "@/lib/constants";

const README = new URL("../../../README.md", import.meta.url);

describe("Footer", () => {
  const html = renderToStaticMarkup(<Footer />);

  it("shows the disclaimer verbatim", () => {
    expect(html).toContain(`<p class="disclaimer">${DISCLAIMER}</p>`);
  });

  it("uses the project's disclaimer, word for word (as in the README)", () => {
    const readme = readFileSync(README, "utf-8").split(/\s+/).join(" ");
    expect(readme).toContain(DISCLAIMER);
    expect(DISCLAIMER).toBe(
      "Layanan ini tidak resmi dan hanya meneruskan data dari BMKG. Notifikasi bisa terlambat " +
        "atau tidak terkirim. Untuk informasi resmi dan arahan keselamatan, ikuti BMKG " +
        "(bmkg.go.id / aplikasi InfoBMKG) dan BPBD setempat.",
    );
  });

  it('links "Sumber: BMKG" to bmkg.go.id', () => {
    expect(html).toMatch(/<a href="https:\/\/www\.bmkg\.go\.id"[^>]*>Sumber: BMKG<\/a>/);
  });

  it("credits OpenStreetMap for the map tiles", () => {
    expect(html).toContain("OpenStreetMap");
  });
});
