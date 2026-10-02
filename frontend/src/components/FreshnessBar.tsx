"use client";

import { useEffect, useState } from "react";

import { api, errorMessage } from "@/lib/api";
import { assessFreshness, STALE_BANNER, UNREACHABLE_BANNER } from "@/lib/freshness";
import type { StatusResponse } from "@/lib/types";

const REFRESH_MS = 60_000;
const TICK_MS = 30_000;

/** "Data terakhir diperbarui X menit lalu", plus a banner when the data may be outdated or
 * the API can't be reached. Shown on every page. */
export function FreshnessBar() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    let controller = new AbortController();
    const load = async () => {
      controller.abort();
      controller = new AbortController();
      try {
        const next = await api.getStatus(controller.signal);
        setStatus(next);
        setError(null);
      } catch (caught) {
        if (controller.signal.aborted) return;
        setError(caught);
      }
      setNow(new Date());
      setLoaded(true);
    };
    void load();
    const refresh = setInterval(() => void load(), REFRESH_MS);
    const tick = setInterval(() => setNow(new Date()), TICK_MS);
    return () => {
      controller.abort();
      clearInterval(refresh);
      clearInterval(tick);
    };
  }, []);

  if (!loaded) {
    return (
      <div className="freshness" role="status">
        <div className="container">Memeriksa pembaruan data…</div>
      </div>
    );
  }

  const freshness = status ? assessFreshness(status, now) : null;
  return (
    <div className="freshness" role="status">
      <div className="container">
        {freshness && (
          <p className="freshness-line">
            {freshness.updatedAtWib ? (
              <span title={freshness.updatedAtWib}>{freshness.updatedText}</span>
            ) : (
              freshness.updatedText
            )}
          </p>
        )}
        {error !== null ? (
          <p className="banner banner-error">
            {status ? UNREACHABLE_BANNER : `${UNREACHABLE_BANNER} (${errorMessage(error)})`}
          </p>
        ) : (
          freshness?.stale && <p className="banner banner-warning">{STALE_BANNER}</p>
        )}
      </div>
    </div>
  );
}
