"use client";
import { useEffect, useRef } from "react";
import type { Media } from "../lib/types";
import "leaflet/dist/leaflet.css";
export default function ArchiveMap({
  items,
  onSelect,
}: {
  items: Media[];
  onSelect: (item: Media) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const select = useRef(onSelect);
  select.current = onSelect;
  useEffect(() => {
    let disposed = false;
    let map: import("leaflet").Map | undefined;
    let resizeTimer: ReturnType<typeof setTimeout> | undefined;
    import("leaflet").then((L) => {
      if (disposed || !root.current) return;
      map = L.map(root.current, { scrollWheelZoom: false }).setView(
        [35, 15],
        2,
      );
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
        maxZoom: 19,
      }).addTo(map);
      const bounds: L.LatLngExpression[] = [];
      for (const item of items) {
        if (item.latitude === null || item.longitude === null) continue;
        const point: [number, number] = [item.latitude, item.longitude];
        bounds.push(point);
        const label = document.createElement("div");
        const title = document.createElement("strong");
        title.textContent = "@" + (item.username || "Без имени");
        label.appendChild(title);
        const button = document.createElement("button");
        button.textContent = "Открыть запись";
        button.className = "map-open";
        button.addEventListener("click", () => select.current(item));
        label.appendChild(button);
        L.circleMarker(point, {
          radius: 6,
          color: "#fff",
          weight: 2,
          fillColor: "#0f766e",
          fillOpacity: 0.9,
        })
          .addTo(map)
          .bindPopup(label);
      }
      if (bounds.length)
        map.fitBounds(L.latLngBounds(bounds), {
          padding: [36, 36],
          maxZoom: 12,
        });
      resizeTimer = setTimeout(() => {
        if (!disposed) map?.invalidateSize();
      }, 100);
    });
    return () => {
      disposed = true;
      clearTimeout(resizeTimer);
      map?.stop();
      map?.remove();
      map = undefined;
    };
  }, [items]);
  return (
    <div
      className="map-canvas"
      ref={root}
      role="region"
      aria-label="Карта координат фотографий"
    />
  );
}
