import { BMKG_URL, POTENTIAL_LABEL, SOURCE_NOTICE } from "@/lib/constants";
import { formatDepth, formatDistance, formatMagnitude, formatWib } from "@/lib/format";
import type { Earthquake } from "@/lib/types";

/** One quake's BMKG values, verbatim, with labels. Used in map popups. */
export function QuakeDetails({ quake }: { quake: Earthquake }) {
  return (
    <div className="quake-details">
      <p className="quake-title">
        <span className="quake-magnitude">M {formatMagnitude(quake.magnitude)}</span>{" "}
        {quake.region}
      </p>
      <dl>
        <dt>Waktu</dt>
        <dd>{formatWib(quake.occurred_at)}</dd>
        <dt>Kedalaman</dt>
        <dd>{formatDepth(quake.depth_km)}</dd>
        <dt>{POTENTIAL_LABEL}</dt>
        <dd>{quake.potential ?? "Tidak ada keterangan"}</dd>
        {quake.felt && (
          <>
            <dt>Dirasakan (MMI)</dt>
            <dd>{quake.felt}</dd>
          </>
        )}
        {quake.distance_km !== null && quake.distance_km !== undefined && (
          <>
            <dt>Jarak dari titik Anda</dt>
            <dd>{formatDistance(quake.distance_km)}</dd>
          </>
        )}
        <dt>Feed BMKG</dt>
        <dd>{quake.source_feeds.join(", ") || "-"}</dd>
      </dl>
      {quake.shakemap_url && (
        <p>
          <a href={quake.shakemap_url} target="_blank" rel="noopener noreferrer">
            Lihat shakemap (BMKG)
          </a>
        </p>
      )}
      <p className="quake-source">
        <a href={BMKG_URL} target="_blank" rel="noopener noreferrer">
          {SOURCE_NOTICE}
        </a>
      </p>
    </div>
  );
}
