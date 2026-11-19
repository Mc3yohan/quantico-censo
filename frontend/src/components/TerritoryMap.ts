/**
 * Full-Viewport Interactive GIS Map powered by Leaflet (preferCanvas + Zero-API-Key Tile Layers).
 * Supports Streets (OpenStreetMap), Satellite (Esri World Imagery), and GIS (Esri World Street Map),
 * organic territory polygons, teardrop GIS pins, smooth flyTo, manual drop-pin mode, and SOS radar.
 */
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { el } from "../shared/dom";
import type { BaseMapStyle, MapPin, MapSnapshot } from "../shared/types";

const TERRITORY_COLORS = ["#0062CC", "#0D9488", "#6366F1", "#D97706"];

const TILE_CONFIGS: Record<BaseMapStyle, { url: string; attribution: string; maxZoom: number }> = {
  streets: {
    url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>',
    maxZoom: 19,
  },
  satellite: {
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attribution: "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics",
    maxZoom: 19,
  },
  gis: {
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    attribution: "Tiles &copy; Esri &mdash; Esri World Street Map",
    maxZoom: 19,
  },
};

function createPinSvg(color: string, glyph: string): string {
  return `<div class="qc-gis-pin" style="--pin-color:${color}">
    <svg width="28" height="36" viewBox="0 0 28 36" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M14 1C6.82 1 1 6.82 1 14C1 23.25 14 35 14 35C14 35 27 23.25 27 14C27 6.82 21.18 1 14 1Z" fill="${color}" stroke="#FFFFFF" stroke-width="2"/>
      <circle cx="14" cy="13.5" r="5.2" fill="#FFFFFF"/>
    </svg>
    <span class="qc-gis-glyph">${glyph}</span>
  </div>`;
}

function markerHtml(pin: MapPin): string {
  if (pin.kind === "sos") {
    return `<div class="qc-pin-sos"><span class="qc-pulse"></span><span class="qc-pulse delay"></span><span class="qc-core">SOS</span></div>`;
  }
  if (pin.kind === "audit") {
    const isAnomaly =
      pin.status === "quarantined" ||
      pin.geofence_status === "outside_territory" ||
      pin.geofence_status === "gps_drift" ||
      pin.stockout_alert;
    return isAnomaly ? createPinSvg("#EA580C", "!") : createPinSvg("#059669", "✓");
  }
  return createPinSvg("#0062CC", "");
}

export interface TerritoryMapCallbacks {
  onPinSelect: (pin: MapPin) => void;
  onMapClick?: (lat: number, lon: number) => void;
}

export class TerritoryMap {
  private map: L.Map;
  private tileLayer: L.TileLayer | null = null;
  private polygonLayer = L.layerGroup();
  private markerLayer = L.layerGroup();
  private currentStyle: BaseMapStyle = "streets";
  private fittedOnce = false;
  private invalidateTimer: number | null = null;
  private cb: TerritoryMapCallbacks;

  constructor(container: HTMLElement, cb: TerritoryMapCallbacks) {
    this.cb = cb;
    this.map = L.map(container, {
      center: [40.706, -73.982],
      zoom: 12,
      preferCanvas: true,
      zoomControl: true,
      attributionControl: true,
    });

    this.setBaseStyle("streets");
    this.polygonLayer.addTo(this.map);
    this.markerLayer.addTo(this.map);

    this.map.on("click", (ev: L.LeafletMouseEvent) => {
      if (this.cb.onMapClick) {
        this.cb.onMapClick(ev.latlng.lat, ev.latlng.lng);
      }
    });
  }

  getBaseStyle(): BaseMapStyle {
    return this.currentStyle;
  }

  setBaseStyle(style: BaseMapStyle): void {
    this.currentStyle = style;
    if (this.tileLayer) {
      this.map.removeLayer(this.tileLayer);
    }
    const cfg = TILE_CONFIGS[style];
    this.tileLayer = L.tileLayer(cfg.url, {
      maxZoom: cfg.maxZoom,
      attribution: cfg.attribution,
    });
    this.tileLayer.addTo(this.map);
  }

  render(snapshot: MapSnapshot, filterRegionId: number | null = null): void {
    this.polygonLayer.clearLayers();
    this.markerLayer.clearLayers();

    const bounds = L.latLngBounds([]);
    let colorIdx = 0;

    for (const region of snapshot.regions) {
      if (filterRegionId !== null && region.id !== filterRegionId) continue;
      for (const territory of region.territories) {
        try {
          const geojson = JSON.parse(territory.polygon_geojson) as GeoJSON.GeoJsonObject;
          const color = TERRITORY_COLORS[colorIdx % TERRITORY_COLORS.length];
          colorIdx += 1;

          const layer = L.geoJSON(geojson, {
            style: {
              color,
              weight: 2.2,
              fillColor: color,
              fillOpacity: 0.11,
              dashArray: "6 5",
            },
          });
          layer.bindTooltip(el("span", { text: `${territory.name} (${region.name})` }), {
            sticky: true,
            direction: "top",
            className: "qc-map-tooltip",
          });
          layer.addTo(this.polygonLayer);
          bounds.extend(layer.getBounds());
        } catch (err) {
          console.warn("Invalid territory polygon GeoJSON skipped", territory.code, err);
        }
      }
    }

    for (const pin of snapshot.pins) {
      if (filterRegionId !== null && pin.region_id !== filterRegionId) continue;
      const isSos = pin.kind === "sos";
      const icon = L.divIcon({
        className: "qc-div-icon",
        html: markerHtml(pin),
        iconSize: isSos ? [38, 38] : [28, 36],
        iconAnchor: isSos ? [19, 19] : [14, 35],
      });

      const marker = L.marker([pin.latitude, pin.longitude], {
        icon,
        zIndexOffset: isSos ? 900 : pin.kind === "audit" ? 400 : 100,
      });
      marker.bindTooltip(el("span", { text: `${pin.code} — ${pin.label}` }), {
        direction: "top",
        offset: isSos ? [0, -18] : [0, -34],
        className: "qc-map-tooltip",
      });
      marker.on("click", () => this.cb.onPinSelect(pin));
      marker.addTo(this.markerLayer);
      bounds.extend([pin.latitude, pin.longitude]);
    }

    if (!this.fittedOnce && bounds.isValid()) {
      this.map.fitBounds(bounds, { padding: [36, 36], maxZoom: 13 });
      this.fittedOnce = true;
    }
  }

  flyTo(lat: number, lon: number, zoom = 15): void {
    this.map.flyTo([lat, lon], zoom, { duration: 0.55 });
  }

  fitRegion(snapshot: MapSnapshot, regionId: number | null): void {
    const bounds = L.latLngBounds([]);
    for (const region of snapshot.regions) {
      if (regionId !== null && region.id !== regionId) continue;
      for (const territory of region.territories) {
        try {
          const layer = L.geoJSON(JSON.parse(territory.polygon_geojson) as GeoJSON.GeoJsonObject);
          bounds.extend(layer.getBounds());
        } catch (err) {
          console.warn("Could not fit territory bounds", territory.code, err);
        }
      }
    }
    if (bounds.isValid()) {
      this.map.flyToBounds(bounds, { padding: [36, 36], maxZoom: 13, duration: 0.5 });
    }
  }

  invalidate(): void {
    if (this.invalidateTimer !== null) {
      window.clearTimeout(this.invalidateTimer);
    }
    this.invalidateTimer = window.setTimeout(() => {
      this.invalidateTimer = null;
      this.map.invalidateSize();
    }, 80);
  }

  destroy(): void {
    if (this.invalidateTimer !== null) {
      window.clearTimeout(this.invalidateTimer);
      this.invalidateTimer = null;
    }
    this.map.remove();
  }
}
