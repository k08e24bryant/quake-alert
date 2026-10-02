"use client";

import "leaflet/dist/leaflet.css";

import {
  AttributionControl,
  Circle,
  CircleMarker,
  MapContainer,
  TileLayer,
  useMapEvents,
} from "react-leaflet";

import {
  AREA_STYLE,
  INDONESIA_BOUNDS,
  LEAFLET_PREFIX,
  OSM_ATTRIBUTION,
  OSM_TILES,
  POINT_STYLE,
} from "@/lib/mapConfig";

export interface PickedPoint {
  lat: number;
  lon: number;
}

interface PointPickerProps {
  point: PickedPoint | null;
  radiusKm: number;
  onPick: (point: PickedPoint) => void;
}

/** Click the map to choose the centre of a radius filter. The coordinate inputs next to it
 * do the same from the keyboard. */
export default function PointPicker({ point, radiusKm, onPick }: PointPickerProps) {
  return (
    <div role="region" aria-label="Peta untuk memilih titik pusat radius. Klik untuk memilih.">
      <MapContainer
        className="map map-small"
        bounds={INDONESIA_BOUNDS}
        scrollWheelZoom={false}
        attributionControl={false}
      >
        <TileLayer url={OSM_TILES} attribution={OSM_ATTRIBUTION} />
        <AttributionControl prefix={LEAFLET_PREFIX} />
        <ClickToPick onPick={onPick} />
        {point && (
          <>
            <Circle center={[point.lat, point.lon]} radius={radiusKm * 1000} pathOptions={AREA_STYLE} />
            <CircleMarker center={[point.lat, point.lon]} radius={6} pathOptions={POINT_STYLE} />
          </>
        )}
      </MapContainer>
    </div>
  );
}

function ClickToPick({ onPick }: { onPick: (point: PickedPoint) => void }) {
  useMapEvents({
    click: (event) => onPick({ lat: event.latlng.lat, lon: event.latlng.lng }),
  });
  return null;
}
