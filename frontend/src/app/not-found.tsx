import Link from "next/link";

export default function NotFound() {
  return (
    <>
      <h1>Halaman tidak ditemukan</h1>
      <p>
        <Link href="/">Kembali ke peta gempa</Link>
      </p>
    </>
  );
}
