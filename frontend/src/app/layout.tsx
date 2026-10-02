import "./globals.css";

import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { Footer } from "@/components/Footer";
import { FreshnessBar } from "@/components/FreshnessBar";
import { SiteHeader } from "@/components/SiteHeader";
import { SITE_NAME } from "@/lib/constants";

export const metadata: Metadata = {
  title: { default: `${SITE_NAME} - data gempa dari BMKG`, template: `%s - ${SITE_NAME}` },
  description:
    "Peta dan riwayat gempa di Indonesia dari data terbuka BMKG. Layanan tidak resmi yang " +
    "meneruskan data BMKG.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  colorScheme: "light dark",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#111418" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="id">
      <body>
        <a href="#konten" className="skip-link">
          Langsung ke konten
        </a>
        <SiteHeader />
        <FreshnessBar />
        <main id="konten" tabIndex={-1} className="container">
          {children}
        </main>
        <Footer />
      </body>
    </html>
  );
}
