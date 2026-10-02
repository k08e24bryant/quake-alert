import { QuakeExplorer } from "@/components/QuakeExplorer";

export default function MapPage() {
  return (
    <>
      <h1>Peta gempa terkini</h1>
      <p className="lead">
        Gempa yang dipublikasikan BMKG. Klik penanda untuk melihat detail, atau gunakan daftar di
        bawah peta.
      </p>
      <QuakeExplorer />
    </>
  );
}
