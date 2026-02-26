"use client";

import { useEffect, useRef } from "react";
import L from "leaflet";
import { Hospital, Patient, AttemptEvent } from "../types";

const BUSAN_CENTER: [number, number] = [35.1796, 129.0756];

const SEVERITY_COLORS: Record<number, string> = {
  1: "#22c55e",
  2: "#84cc16",
  3: "#eab308",
  4: "#f97316",
  5: "#ef4444",
};

interface Props {
  hospitals: Hospital[];
  patients: Patient[];
  /** Flat list of hop events to draw (grows over time via SSE). */
  hops: AttemptEvent[];
  label: string;
}

export default function SimulationMap({
  hospitals,
  patients,
  hops,
  label,
}: Props) {
  const mapRef = useRef<L.Map | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const baseLayerRef = useRef<L.LayerGroup | null>(null);
  const hopsLayerRef = useRef<L.LayerGroup | null>(null);
  // Track how many hops we've already drawn so we only append new ones
  const drawnHopCountRef = useRef(0);

  // Initialize map
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = L.map(containerRef.current, {
      center: BUSAN_CENTER,
      zoom: 12,
      zoomControl: false,
    });
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
      maxZoom: 18,
    }).addTo(map);
    L.control.zoom({ position: "bottomright" }).addTo(map);
    mapRef.current = map;
    baseLayerRef.current = L.layerGroup().addTo(map);
    hopsLayerRef.current = L.layerGroup().addTo(map);

    // Leaflet needs a size recalc after the container settles
    setTimeout(() => map.invalidateSize(), 200);

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // Redraw base layer (hospitals + patients) when data changes
  useEffect(() => {
    const layers = baseLayerRef.current;
    if (!layers) return;
    layers.clearLayers();

    hospitals.forEach((h) => {
      const occupancyPct = Math.round(
        (h.current_occupancy / Math.max(h.max_capacity, 1)) * 100
      );
      const marker = L.circleMarker([h.lat, h.lng], {
        radius: 8,
        fillColor: "#3b82f6",
        color: "#1e40af",
        weight: 2,
        fillOpacity: 0.8,
      });
      marker.bindTooltip(
        `<b>${h.hospital_name}</b><br/>Capacity: ${h.current_occupancy}/${h.max_capacity} (${occupancyPct}%)`,
        { direction: "top" }
      );
      layers.addLayer(marker);
    });

    // Patient start locations — always visible
    patients.forEach((p) => {
      const color = SEVERITY_COLORS[p.severity] || "#888";
      const marker = L.circleMarker([p.lat, p.lng], {
        radius: 5,
        fillColor: color,
        color: "#fff",
        weight: 1.5,
        fillOpacity: 0.9,
      });
      marker.bindTooltip(
        `Patient ${p.patient_id} (severity: ${p.severity})`,
        { direction: "top" }
      );
      layers.addLayer(marker);
    });

    // Reset hop drawing when base data changes (new scenario)
    if (hopsLayerRef.current) {
      hopsLayerRef.current.clearLayers();
    }
    drawnHopCountRef.current = 0;

    // Fit map bounds to hospitals + patients
    const map = mapRef.current;
    if (map && (hospitals.length > 0 || patients.length > 0)) {
      const points: [number, number][] = [
        ...hospitals.map((h): [number, number] => [h.lat, h.lng]),
        ...patients.map((p): [number, number] => [p.lat, p.lng]),
      ];
      if (points.length > 0) {
        map.invalidateSize();
        map.fitBounds(L.latLngBounds(points).pad(0.1));
      }
    }
  }, [hospitals, patients]);

  // Incrementally draw new hops as they arrive
  useEffect(() => {
    const layer = hopsLayerRef.current;
    if (!layer) return;

    const patientMap = new Map(patients.map((p) => [p.patient_id, p]));

    // Only draw hops we haven't drawn yet
    for (let i = drawnHopCountRef.current; i < hops.length; i++) {
      const hop = hops[i];

      // Find previous position: either the previous hop for this patient
      // or the patient's start location
      let prevLat: number;
      let prevLng: number;

      // Look backwards for the previous hop of the same patient
      let found = false;
      for (let j = i - 1; j >= 0; j--) {
        if (hops[j].patient_id === hop.patient_id) {
          prevLat = hops[j].lat;
          prevLng = hops[j].lng;
          found = true;
          break;
        }
      }
      if (!found) {
        const pat = patientMap.get(hop.patient_id);
        prevLat = pat?.lat ?? hop.lat;
        prevLng = pat?.lng ?? hop.lng;
      }

      const color = hop.accepted ? "#22c55e" : "#ef4444";
      const line = L.polyline(
        [
          [prevLat!, prevLng!],
          [hop.lat, hop.lng],
        ],
        {
          color,
          weight: hop.accepted ? 3 : 1.5,
          opacity: hop.accepted ? 0.9 : 0.4,
          dashArray: hop.accepted ? undefined : "4 4",
        }
      );
      layer.addLayer(line);
    }

    drawnHopCountRef.current = hops.length;
  }, [hops, patients]);

  return (
    <div className="flex flex-col h-full">
      <h3 className="text-lg font-semibold mb-2 text-center">{label}</h3>
      <div ref={containerRef} className="flex-1 min-h-[400px] rounded-lg" />
    </div>
  );
}
