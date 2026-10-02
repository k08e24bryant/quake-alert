"use client";

import dynamic from "next/dynamic";

/** Riwayat renders in the browser only: its default date range is "the last 30 days" in
 * the user's present, which a page prerendered at build time can't know. */
export const HistoryLoader = dynamic(
  () => import("@/components/HistoryExplorer").then((module) => module.HistoryExplorer),
  { ssr: false, loading: () => <p className="load-status">Memuat riwayat…</p> },
);
