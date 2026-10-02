import type { Metadata } from "next";

import { config } from "@/lib/config";

export const metadata: Metadata = { title: "Cara berlangganan" };

const COMMANDS = [
  { command: "/start", effect: "Penjelasan bot, daftar perintah, dan tombol untuk membagikan lokasi." },
  {
    command: "Bagikan lokasi",
    effect:
      "Mulai berlangganan untuk lokasi itu, atau pindahkan langganan Anda ke lokasi baru. " +
      "Lokasi disimpan dalam bentuk dibulatkan (sekitar 1 km).",
  },
  { command: "/radius <km>", effect: "Ubah radius, 10 sampai 1000 km. Contoh: /radius 150" },
  {
    command: "/minmag <nilai>",
    effect: "Ubah magnitudo minimal, 2.0 sampai 9.0. Contoh: /minmag 4.5",
  },
  { command: "/list", effect: "Lihat lokasi, radius, dan magnitudo minimal langganan Anda." },
  { command: "/stop", effect: "Berhenti berlangganan dan hapus semua data Anda." },
] as const;

export default function SubscribePage() {
  const docsUrl = config.repoUrl ? `${config.repoUrl}/blob/main/docs/webhooks.md` : null;
  return (
    <>
      <h1>Cara berlangganan</h1>
      <p className="lead">
        Dapatkan info gempa di sekitar lokasi pilihan Anda. Info dikirim setelah BMKG
        mempublikasikan data gempa, dan bisa terlambat atau tidak terkirim.
      </p>

      <section aria-labelledby="telegram-title" className="card">
        <h2 id="telegram-title">Lewat Telegram</h2>
        <ol className="steps">
          <li>
            Buka bot{" "}
            {config.telegramBotUrl ? (
              <a href={config.telegramBotUrl} target="_blank" rel="noopener noreferrer">
                di Telegram
              </a>
            ) : (
              <span>(tautan bot belum dikonfigurasi)</span>
            )}{" "}
            lalu kirim <code>/start</code>.
          </li>
          <li>Bagikan lokasi Anda lewat tombol di bot atau menu lampiran, Lokasi.</li>
          <li>
            Pengaturan awal: radius 200 km dan magnitudo minimal 4.0. Ubah kapan saja dengan
            perintah di bawah.
          </li>
        </ol>
        <div className="table-wrap">
          <table>
            <caption>Perintah bot</caption>
            <thead>
              <tr>
                <th scope="col">Perintah</th>
                <th scope="col">Fungsi</th>
              </tr>
            </thead>
            <tbody>
              {COMMANDS.map(({ command, effect }) => (
                <tr key={command}>
                  <td className="nowrap">
                    <code>{command}</code>
                  </td>
                  <td>{effect}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section aria-labelledby="webhook-title" className="card">
        <h2 id="webhook-title">Lewat webhook (untuk pengembang)</h2>
        <p>
          Sistem Anda dapat menerima info gempa sebagai permintaan <code>POST</code> JSON yang
          ditandatangani (HMAC-SHA256).
        </p>
        <ol className="steps">
          <li>
            Daftarkan URL HTTPS Anda dengan <code>POST /v1/subscriptions/webhook</code>. Jawaban
            berisi <code>signing_secret</code> dan <code>manage_token</code>, hanya sekali.
          </li>
          <li>Simpan <code>signing_secret</code> di penerima Anda.</li>
          <li>
            Panggil <code>POST /v1/subscriptions/webhook/&#123;id&#125;/verify</code>. Penerima harus
            membalas tantangan (challenge) yang dikirim. Setelah itu langganan aktif.
          </li>
        </ol>
        <p>
          {docsUrl ? (
            <a href={docsUrl} target="_blank" rel="noopener noreferrer">
              Dokumentasi webhook lengkap (bahasa Inggris)
            </a>
          ) : (
            "Dokumentasi webhook ada di docs/webhooks.md di repositori."
          )}
        </p>
      </section>
    </>
  );
}
