"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";

import { MagnitudeLegend } from "@/components/MagnitudeLegend";
import type { UserArea } from "@/components/QuakeMap";
import { api, errorMessage, listUpTo } from "@/lib/api";
import { formatDistance, formatMagnitude, formatWib } from "@/lib/format";
import { geolocationErrorMessage, roundCoordinate } from "@/lib/geo";
import type { Earthquake } from "@/lib/types";

// Leaflet needs `window`: the map is rendered in the browser only.
const QuakeMap = dynamic(() => import("@/components/QuakeMap"), {
  ssr: false,
  loading: () => <div className="map map-placeholder">Memuat peta…</div>,
});

const MAX_ON_MAP = 500;
const RANGES = [
  { days: 1, label: "24 jam terakhir" },
  { days: 3, label: "3 hari terakhir" },
  { days: 7, label: "7 hari terakhir" },
  { days: 30, label: "30 hari terakhir" },
] as const;
const MAGNITUDES = [0, 3, 4, 5, 6] as const;
const NEARBY_RADII = [100, 200, 300, 500] as const;

type Load =
  | { state: "loading" }
  | { state: "error"; error: unknown }
  | { state: "ready"; quakes: Earthquake[]; truncated: boolean };

/** A result belongs to the filters it was loaded for; any other result means "loading". */
interface Keyed<T> {
  key: string;
  value: T;
}

type Locating =
  | { state: "idle" }
  | { state: "locating" }
  | { state: "error"; message: string };

export function QuakeExplorer() {
  const [minMag, setMinMag] = useState<number>(0);
  const [rangeDays, setRangeDays] = useState<number>(7);
  const [position, setPosition] = useState<{ lat: number; lon: number } | null>(null);
  const [nearbyRadius, setNearbyRadius] = useState<number>(300);
  const [locating, setLocating] = useState<Locating>({ state: "idle" });
  const [attempt, setAttempt] = useState(0);
  const requestKey = JSON.stringify([minMag, rangeDays, position, nearbyRadius, attempt]);
  const [result, setResult] = useState<Keyed<Load> | null>(null);
  const [selected, setSelected] = useState<Keyed<string> | null>(null);

  const load: Load = result?.key === requestKey ? result.value : { state: "loading" };
  const selectedId = selected?.key === requestKey ? selected.value : null;
  const userArea: UserArea | null = useMemo(
    () => (position ? { ...position, radiusKm: nearbyRadius } : null),
    [position, nearbyRadius],
  );

  useEffect(() => {
    const controller = new AbortController();
    // Whole minutes, so repeated requests share the API's short-lived cache.
    const now = Math.floor(Date.now() / 60_000) * 60_000;
    const done = (value: Load) => setResult({ key: requestKey, value });
    listUpTo(
      api,
      {
        minMag: minMag > 0 ? minMag : undefined,
        start: new Date(now - rangeDays * 86_400_000).toISOString(),
        lat: position?.lat,
        lon: position?.lon,
        radiusKm: position ? nearbyRadius : undefined,
      },
      MAX_ON_MAP,
      controller.signal,
    )
      .then(({ items, truncated }) => done({ state: "ready", quakes: items, truncated }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) done({ state: "error", error });
      });
    return () => controller.abort();
  }, [minMag, rangeDays, position, nearbyRadius, requestKey]);

  function locate() {
    if (!("geolocation" in navigator)) {
      setLocating({ state: "error", message: geolocationErrorMessage(null) });
      return;
    }
    setLocating({ state: "locating" });
    navigator.geolocation.getCurrentPosition(
      (result) => {
        // Kept in memory only, rounded, and sent nowhere but the API's query string.
        setPosition({
          lat: roundCoordinate(result.coords.latitude),
          lon: roundCoordinate(result.coords.longitude),
        });
        setLocating({ state: "idle" });
      },
      (error) => setLocating({ state: "error", message: geolocationErrorMessage(error.code) }),
      { enableHighAccuracy: false, timeout: 15_000, maximumAge: 300_000 },
    );
  }

  return (
    <div className="stack">
      <form className="filters" onSubmit={(event) => event.preventDefault()}>
        <div className="field">
          <label htmlFor="map-min-mag">Magnitudo minimal</label>
          <select
            id="map-min-mag"
            value={minMag}
            onChange={(event) => setMinMag(Number(event.target.value))}
          >
            {MAGNITUDES.map((value) => (
              <option key={value} value={value}>
                {value === 0 ? "Semua" : `M ${value},0 ke atas`}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="map-range">Rentang waktu</label>
          <select
            id="map-range"
            value={rangeDays}
            onChange={(event) => setRangeDays(Number(event.target.value))}
          >
            {RANGES.map(({ days, label }) => (
              <option key={days} value={days}>
                {label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="map-radius">Radius sekitar saya</label>
          <select
            id="map-radius"
            value={nearbyRadius}
            onChange={(event) => setNearbyRadius(Number(event.target.value))}
          >
            {NEARBY_RADII.map((km) => (
              <option key={km} value={km}>
                {km} km
              </option>
            ))}
          </select>
        </div>
        <div className="field field-actions">
          {position ? (
            <button type="button" className="button" onClick={() => setPosition(null)}>
              Tampilkan seluruh Indonesia
            </button>
          ) : (
            <button
              type="button"
              className="button button-primary"
              onClick={locate}
              disabled={locating.state === "locating"}
            >
              {locating.state === "locating" ? "Menentukan lokasi…" : "Gempa di sekitar saya"}
            </button>
          )}
        </div>
      </form>
      <p className="hint">
        Lokasi hanya dipakai di perangkat ini untuk menyaring gempa, dibulatkan sekitar 1 km, dan
        tidak disimpan.
      </p>
      {locating.state === "error" && (
        <p className="banner banner-warning" role="alert">
          {locating.message}
        </p>
      )}

      <div aria-live="polite" className="load-status">
        {load.state === "loading" && <p>Memuat data gempa…</p>}
        {load.state === "ready" && (
          <p>
            {load.quakes.length === 0
              ? "Tidak ada gempa yang tercatat untuk filter dan rentang waktu ini."
              : `${load.quakes.length} gempa ditampilkan.`}
            {load.truncated && ` Hanya ${MAX_ON_MAP} gempa terbaru yang ditampilkan.`}
          </p>
        )}
      </div>

      {load.state === "error" ? (
        // Never an empty map: that would look like "no quakes".
        <div className="error-panel" role="alert">
          <p>{errorMessage(load.error)}</p>
          <button type="button" className="button" onClick={() => setAttempt((n) => n + 1)}>
            Coba lagi
          </button>
        </div>
      ) : load.state === "loading" ? (
        <div className="map map-placeholder">Memuat data gempa…</div>
      ) : (
        <>
          <div id="peta-gempa">
            <QuakeMap quakes={load.quakes} selectedId={selectedId} userArea={userArea} />
          </div>
          <MagnitudeLegend />
          {load.quakes.length > 0 && (
            <section aria-labelledby="quake-list-title">
              <h2 id="quake-list-title">Daftar gempa di peta</h2>
              <ol className="quake-list">
                {load.quakes.map((quake) => (
                  <li key={quake.id}>
                    <div>
                      <strong>M {formatMagnitude(quake.magnitude)}</strong> {quake.region}
                      <br />
                      <span className="muted">
                        {formatWib(quake.occurred_at)}
                        {quake.distance_km !== null &&
                          quake.distance_km !== undefined &&
                          ` · ${formatDistance(quake.distance_km)} dari Anda`}
                      </span>
                    </div>
                    <button
                      type="button"
                      className="button button-small"
                      onClick={() => {
                        setSelected({ key: requestKey, value: quake.id });
                        document.getElementById("peta-gempa")?.scrollIntoView({ block: "center" });
                      }}
                    >
                      Tampilkan di peta
                    </button>
                  </li>
                ))}
              </ol>
            </section>
          )}
        </>
      )}
    </div>
  );
}
