import { MAGNITUDE_BANDS, magnitudeRadius } from "@/lib/magnitude";

/** The marker scale. Size and colour only show magnitude; they say nothing about impact. */
export function MagnitudeLegend() {
  return (
    <figure className="legend">
      <figcaption>Ukuran dan warna penanda menurut magnitudo</figcaption>
      <ul>
        {MAGNITUDE_BANDS.map((band) => {
          const size = magnitudeRadius(Number.isFinite(band.min) ? band.min : 2) * 2;
          return (
            <li key={band.label}>
              <span
                className="legend-dot"
                aria-hidden="true"
                style={{
                  width: size,
                  height: size,
                  background: band.color,
                  // Keep the row height stable whatever the dot size.
                  marginInline: Math.max(0, (24 - size) / 2),
                }}
              />
              M {band.label}
            </li>
          );
        })}
      </ul>
    </figure>
  );
}
