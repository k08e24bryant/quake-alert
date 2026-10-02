"use client";

import dynamic from "next/dynamic";
import { type FormEvent, useEffect, useState } from "react";

import type { PickedPoint } from "@/components/PointPicker";
import { api, errorMessage } from "@/lib/api";
import { POTENTIAL_LABEL } from "@/lib/constants";
import {
  formatDepth,
  formatDistance,
  formatMagnitude,
  formatWib,
  shiftDate,
  wibDateString,
} from "@/lib/format";
import { roundCoordinate } from "@/lib/geo";
import {
  buildHistoryQuery,
  type HistoryForm,
  RADIUS_OPTIONS,
} from "@/lib/historyFilters";
import type { Earthquake, EarthquakeQuery } from "@/lib/types";

const PointPicker = dynamic(() => import("@/components/PointPicker"), {
  ssr: false,
  loading: () => <div className="map map-small map-placeholder">Memuat peta…</div>,
});

const PAGE_SIZE = 25;

/** One applied search: a new object on every "Terapkan filter" or retry, and results
 * count only for the search they were loaded for. */
interface Search {
  query: EarthquakeQuery;
}

interface Results {
  search: Search;
  quakes: Earthquake[];
  nextCursor: string | null;
  /** The first page failed. */
  error: unknown;
}

function initialForm(): HistoryForm {
  const today = wibDateString(new Date());
  return {
    minMag: "",
    maxMag: "",
    startDate: shiftDate(today, -29),
    endDate: today,
    lat: "",
    lon: "",
    radiusKm: 200,
  };
}

export function HistoryExplorer() {
  const [form, setForm] = useState<HistoryForm>(initialForm);
  const [formError, setFormError] = useState<string | null>(null);
  // The default filters (last 30 days) apply on arrival. This component only renders in
  // the browser (see HistoryLoader), so "today" is the user's, not the build server's.
  const [search, setSearch] = useState<Search | null>(() => {
    const built = buildHistoryQuery(initialForm());
    return built.ok ? { query: built.query } : null;
  });
  const [results, setResults] = useState<Results | null>(null);
  const [more, setMore] = useState<{ loading: boolean; error: unknown }>({
    loading: false,
    error: null,
  });

  const update = (patch: Partial<HistoryForm>) => setForm((current) => ({ ...current, ...patch }));
  const point: PickedPoint | null =
    form.lat.trim() !== "" &&
    form.lon.trim() !== "" &&
    Number.isFinite(Number(form.lat)) &&
    Number.isFinite(Number(form.lon))
      ? { lat: Number(form.lat), lon: Number(form.lon) }
      : null;

  // The first page of each search.
  useEffect(() => {
    if (!search) return;
    const controller = new AbortController();
    api
      .listEarthquakes({ ...search.query, limit: PAGE_SIZE }, controller.signal)
      .then((page) =>
        setResults({ search, quakes: page.data, nextCursor: page.next_cursor, error: null }),
      )
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setResults({ search, quakes: [], nextCursor: null, error });
        }
      });
    return () => controller.abort();
  }, [search]);

  const current = results !== null && results.search === search ? results : null;
  const loadingFirst = search !== null && current === null;
  const loading = loadingFirst || more.loading;

  function submit(event: FormEvent) {
    event.preventDefault();
    const built = buildHistoryQuery(form);
    if (!built.ok) {
      setFormError(built.error);
      return;
    }
    setFormError(null);
    setMore({ loading: false, error: null });
    setSearch({ query: built.query });
  }

  async function loadMore() {
    if (!current?.nextCursor) return;
    setMore({ loading: true, error: null });
    try {
      const page = await api.listEarthquakes({
        ...current.search.query,
        limit: PAGE_SIZE,
        cursor: current.nextCursor,
      });
      // Only if the user hasn't started another search meanwhile.
      setResults((latest) =>
        latest?.search === current.search
          ? {
              ...latest,
              quakes: [...latest.quakes, ...page.data],
              nextCursor: page.next_cursor,
            }
          : latest,
      );
      setMore({ loading: false, error: null });
    } catch (error) {
      setMore({ loading: false, error });
    }
  }

  const showDistance = current?.search.query.lat !== undefined;
  const shownError = current?.error ?? more.error;

  return (
    <div className="stack">
      <form className="filters filters-history" onSubmit={submit} noValidate>
        <div className="field">
          <label htmlFor="h-min-mag">Magnitudo minimal</label>
          <input
            id="h-min-mag"
            inputMode="decimal"
            placeholder="mis. 4,0"
            value={form.minMag}
            onChange={(event) => update({ minMag: event.target.value })}
          />
        </div>
        <div className="field">
          <label htmlFor="h-max-mag">Magnitudo maksimal</label>
          <input
            id="h-max-mag"
            inputMode="decimal"
            placeholder="kosongkan jika bebas"
            value={form.maxMag}
            onChange={(event) => update({ maxMag: event.target.value })}
          />
        </div>
        <div className="field">
          <label htmlFor="h-start">Dari tanggal (WIB)</label>
          <input
            id="h-start"
            type="date"
            value={form.startDate}
            onChange={(event) => update({ startDate: event.target.value })}
          />
        </div>
        <div className="field">
          <label htmlFor="h-end">Sampai tanggal (WIB)</label>
          <input
            id="h-end"
            type="date"
            value={form.endDate}
            onChange={(event) => update({ endDate: event.target.value })}
          />
        </div>

        <fieldset className="field-wide">
          <legend>Radius di sekitar titik (opsional)</legend>
          <p className="hint">Klik peta untuk memilih titik pusat, atau isi koordinatnya.</p>
          <PointPicker
            point={point}
            radiusKm={form.radiusKm}
            onPick={(picked) =>
              update({
                lat: String(roundCoordinate(picked.lat)),
                lon: String(roundCoordinate(picked.lon)),
              })
            }
          />
          <div className="filters-inline">
            <div className="field">
              <label htmlFor="h-lat">Lintang</label>
              <input
                id="h-lat"
                inputMode="decimal"
                placeholder="mis. -6,21"
                value={form.lat}
                onChange={(event) => update({ lat: event.target.value })}
              />
            </div>
            <div className="field">
              <label htmlFor="h-lon">Bujur</label>
              <input
                id="h-lon"
                inputMode="decimal"
                placeholder="mis. 106,85"
                value={form.lon}
                onChange={(event) => update({ lon: event.target.value })}
              />
            </div>
            <div className="field">
              <label htmlFor="h-radius">Radius</label>
              <select
                id="h-radius"
                value={form.radiusKm}
                onChange={(event) => update({ radiusKm: Number(event.target.value) })}
              >
                {RADIUS_OPTIONS.map((km) => (
                  <option key={km} value={km}>
                    {km} km
                  </option>
                ))}
              </select>
            </div>
            <div className="field field-actions">
              <button
                type="button"
                className="button"
                onClick={() => update({ lat: "", lon: "" })}
                disabled={form.lat === "" && form.lon === ""}
              >
                Hapus titik
              </button>
            </div>
          </div>
        </fieldset>

        <div className="field field-actions field-wide">
          <button type="submit" className="button button-primary">
            Terapkan filter
          </button>
        </div>
      </form>

      {formError && (
        <p className="banner banner-warning" role="alert">
          {formError}
        </p>
      )}

      <div aria-live="polite" className="load-status">
        {loading && <p>Memuat data gempa…</p>}
        {current && !current.error && !loading && (
          <p>
            {current.quakes.length === 0
              ? "Tidak ada gempa yang cocok dengan filter ini."
              : `Menampilkan ${current.quakes.length} gempa.` +
                (current.nextCursor ? "" : " Semua hasil sudah dimuat.")}
          </p>
        )}
      </div>

      {shownError !== null && shownError !== undefined && (
        <div className="error-panel" role="alert">
          <p>{errorMessage(shownError)}</p>
          <button
            type="button"
            className="button"
            onClick={() =>
              current?.error ? setSearch({ query: current.search.query }) : void loadMore()
            }
          >
            Coba lagi
          </button>
        </div>
      )}

      {current && current.quakes.length > 0 && (
        <div className="table-wrap">
          <table className="history-table">
            <caption>Riwayat gempa dari data BMKG, terbaru di atas. Waktu dalam WIB.</caption>
            <thead>
              <tr>
                <th scope="col">Waktu (WIB)</th>
                <th scope="col">Magnitudo</th>
                <th scope="col">Kedalaman</th>
                <th scope="col">Wilayah</th>
                <th scope="col">{POTENTIAL_LABEL}</th>
                {showDistance && <th scope="col">Jarak</th>}
                <th scope="col">Shakemap</th>
              </tr>
            </thead>
            <tbody>
              {current.quakes.map((quake) => (
                <tr key={quake.id}>
                  <td className="nowrap">{formatWib(quake.occurred_at)}</td>
                  <td>M {formatMagnitude(quake.magnitude)}</td>
                  <td className="nowrap">{formatDepth(quake.depth_km)}</td>
                  <td>{quake.region}</td>
                  <td>{quake.potential ?? "Tidak ada keterangan"}</td>
                  {showDistance && (
                    <td className="nowrap">
                      {quake.distance_km !== null && quake.distance_km !== undefined
                        ? formatDistance(quake.distance_km)
                        : "-"}
                    </td>
                  )}
                  <td>
                    {quake.shakemap_url ? (
                      <a href={quake.shakemap_url} target="_blank" rel="noopener noreferrer">
                        Lihat
                      </a>
                    ) : (
                      "-"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {current?.nextCursor && (
        <button
          type="button"
          className="button button-primary load-more"
          onClick={() => void loadMore()}
          disabled={loading}
        >
          {loading ? "Memuat…" : "Muat lebih banyak"}
        </button>
      )}
    </div>
  );
}
