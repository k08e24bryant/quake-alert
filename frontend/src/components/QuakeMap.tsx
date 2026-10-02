"use client";

import "leaflet/dist/leaflet.css";

import type { CircleMarker as LeafletCircleMarker } from "leaflet";
import { type RefObject, useEffect, useRef } from "react";
import {
  AttributionControl,
  Circle,
  CircleMarker,
  MapContainer,
  Popup,
  TileLayer,
  useMap,
} from "react-leaflet";

import { QuakeDetails } from "@/components/QuakeDetails";
import { formatMagnitude } from "@/lib/format";
import { magnitudeColor, magnitudeRadius } from "@/lib/magnitude";
import {
  AREA_STYLE,
  INDONESIA_BOUNDS,
  LEAFLET_PREFIX,
  OSM_ATTRIBUTION,
  OSM_TILES,
  POINT_STYLE,
} from "@/lib/mapConfig";
import type { Earthquake } from "@/lib/types";

export interface UserArea {
  lat: number;
  lon: number;
  radiusKm: number;
}

interface QuakeMapProps {
  quakes: Earthquake[];
  selectedId: string | null;
  userArea: UserArea | null;
}

export default function QuakeMap({ quakes, selectedId, userArea }: QuakeMapProps) {
  const markers = useRef(new Map<string, LeafletCircleMarker>());
  // Bigger quakes underneath, so small ones stay clickable.
  const ordered = [...quakes].sort((a, b) => b.magnitude - a.magnitude);

  return (
    <div role="region" aria-label="Peta gempa. Daftar yang sama ada di bawah peta.">
      <MapContainer
        className="map"
        bounds={INDONESIA_BOUNDS}
        scrollWheelZoom={false}
        attributionControl={false}
      >
        <TileLayer url={OSM_TILES} attribution={OSM_ATTRIBUTION} />
        <AttributionControl prefix={LEAFLET_PREFIX} />
        {userArea && (
          <>
            <Circle
              center={[userArea.lat, userArea.lon]}
              radius={userArea.radiusKm * 1000}
              pathOptions={AREA_STYLE}
            />
            <CircleMarker center={[userArea.lat, userArea.lon]} radius={6} pathOptions={POINT_STYLE}>
              <Popup>Lokasi Anda (hanya di perangkat ini, tidak disimpan)</Popup>
            </CircleMarker>
          </>
        )}
        {ordered.map((quake) => (
          <CircleMarker
            key={quake.id}
            center={[quake.latitude, quake.longitude]}
            radius={magnitudeRadius(quake.magnitude)}
            pathOptions={{
              color: "#111827",
              weight: 1,
              fillColor: magnitudeColor(quake.magnitude),
              fillOpacity: 0.8,
            }}
            ref={(marker) => {
              if (marker) markers.current.set(quake.id, marker);
              else markers.current.delete(quake.id);
            }}
            eventHandlers={{
              add: (event) => {
                const element = (event.target as LeafletCircleMarker).getElement();
                element?.setAttribute(
                  "aria-label",
                  `M ${formatMagnitude(quake.magnitude)} ${quake.region}`,
                );
              },
            }}
          >
            <Popup>
              <QuakeDetails quake={quake} />
            </Popup>
          </CircleMarker>
        ))}
        <FitView userArea={userArea} />
        <FocusSelected selectedId={selectedId} markers={markers} />
      </MapContainer>
    </div>
  );
}

/** Zoom to the user's area when one is set; otherwise to Indonesia. Only when the area
 * itself changes, so opening a popup never resets the view. */
function FitView({ userArea }: { userArea: UserArea | null }) {
  const map = useMap();
  const lat = userArea?.lat;
  const lon = userArea?.lon;
  const radiusKm = userArea?.radiusKm;
  useEffect(() => {
    if (lat === undefined || lon === undefined || radiusKm === undefined) {
      map.fitBounds(INDONESIA_BOUNDS);
      return;
    }
    const degrees = radiusKm / 111;
    map.fitBounds([
      [lat - degrees, lon - degrees],
      [lat + degrees, lon + degrees],
    ]);
  }, [map, lat, lon, radiusKm]);
  return null;
}

/** "Tampilkan di peta" from the list: centre the marker and open its popup. */
function FocusSelected({
  selectedId,
  markers,
}: {
  selectedId: string | null;
  markers: RefObject<Map<string, LeafletCircleMarker>>;
}) {
  const map = useMap();
  useEffect(() => {
    if (!selectedId) return;
    const marker = markers.current.get(selectedId);
    if (!marker) return;
    map.setView(marker.getLatLng(), Math.max(map.getZoom(), 7));
    marker.openPopup();
  }, [map, markers, selectedId]);
  return null;
}
