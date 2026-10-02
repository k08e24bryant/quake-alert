"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { SITE_NAME } from "@/lib/constants";

const LINKS = [
  { href: "/", label: "Peta" },
  { href: "/riwayat", label: "Riwayat" },
  { href: "/cara-berlangganan", label: "Cara berlangganan" },
] as const;

export function SiteHeader() {
  const pathname = usePathname();
  return (
    <header className="site-header">
      <div className="container header-inner">
        <Link href="/" className="site-name">
          {SITE_NAME}
          <span className="site-tagline">data gempa dari BMKG</span>
        </Link>
        <nav aria-label="Navigasi utama">
          <ul className="nav-list">
            {LINKS.map(({ href, label }) => (
              <li key={href}>
                <Link href={href} aria-current={pathname === href ? "page" : undefined}>
                  {label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      </div>
    </header>
  );
}
