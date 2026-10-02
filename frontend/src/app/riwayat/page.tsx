import type { Metadata } from "next";

import { HistoryLoader } from "@/components/HistoryLoader";

export const metadata: Metadata = { title: "Riwayat" };

export default function HistoryPage() {
  return (
    <>
      <h1>Riwayat gempa</h1>
      <p className="lead">
        Gempa yang tercatat sejak layanan ini mulai membaca data BMKG. BMKG tidak menyediakan
        arsip lewat data terbukanya, jadi riwayat di sini bisa tidak lengkap.
      </p>
      <HistoryLoader />
    </>
  );
}
