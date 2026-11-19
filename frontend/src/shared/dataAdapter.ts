/**
 * Dual Data Adapter for Quantico Censo:
 *  - LocalSimAdapter: Zero-backend synthetic Retail/PDV engine with organic urban polygons,
 *    point-in-polygon geofencing, RBAC isolation, support tickets, and BroadcastChannel sync.
 *  - HttpApiAdapter: Live Quantico Censo Docker CQRS FastAPI + Redis Pub/Sub WebSocket bridge.
 */

import type {
  AdapterMode,
  AuditSyncResultItem,
  BatchSyncResponse,
  DashboardKpis,
  DataAdapter,
  DispatchRoute,
  FieldAudit,
  FieldAuditInput,
  MapPin,
  MapSnapshot,
  Outlet,
  RealtimeStatus,
  Region,
  SosAlert,
  SupportTicket,
  Territory,
  TriageItem,
  UserProfile,
} from "./types";

function polyGeoJson(ring: number[][]): string {
  return JSON.stringify({ type: "Polygon", coordinates: [ring] });
}

function haversineMeters(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const r = 6_371_000;
  const toRad = (deg: number) => (deg * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * r * Math.atan2(Math.sqrt(a), Math.sqrt(Math.max(0, 1 - a)));
}

function pointInPolygon(lat: number, lon: number, polygonGeoJson: string): boolean {
  const parsed = JSON.parse(polygonGeoJson) as { coordinates: number[][][] };
  const ring = parsed.coordinates[0];
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0];
    const yi = ring[i][1];
    const xj = ring[j][0];
    const yj = ring[j][1];
    const intersect =
      yi > lat !== yj > lat && lon < ((xj - xi) * (lat - yi)) / (yj - yi + 1e-12) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

const DEMO_REGIONS: Region[] = [
  {
    id: 1,
    code: "REG-NORTH",
    name: "Metro North",
    country_code: "US",
    is_active: true,
    territories: [
      {
        id: 1,
        region_id: 1,
        code: "TER-DOWNTOWN",
        name: "Downtown Core",
        polygon_geojson: polyGeoJson([
          [-74.0175, 40.7035],
          [-74.0065, 40.701],
          [-73.992, 40.7068],
          [-73.983, 40.7125],
          [-73.9845, 40.7245],
          [-73.9935, 40.7335],
          [-74.0085, 40.733],
          [-74.0145, 40.7225],
          [-74.0175, 40.7035],
        ]),
        center_lat: 40.7185,
        center_lon: -74.0005,
        is_active: true,
      },
      {
        id: 2,
        region_id: 1,
        code: "TER-TECH",
        name: "Innovation District",
        polygon_geojson: polyGeoJson([
          [-73.977, 40.7045],
          [-73.9635, 40.703],
          [-73.9465, 40.7095],
          [-73.941, 40.7215],
          [-73.9475, 40.7328],
          [-73.961, 40.734],
          [-73.9745, 40.7265],
          [-73.9785, 40.7145],
          [-73.977, 40.7045],
        ]),
        center_lat: 40.7185,
        center_lon: -73.9595,
        is_active: true,
      },
    ],
  },
  {
    id: 2,
    code: "REG-EAST",
    name: "Coastal East",
    country_code: "US",
    is_active: true,
    territories: [
      {
        id: 3,
        region_id: 2,
        code: "TER-HARBOR",
        name: "Harbor Strip",
        polygon_geojson: polyGeoJson([
          [-74.016, 40.673],
          [-74.0035, 40.6675],
          [-73.9875, 40.6705],
          [-73.9815, 40.682],
          [-73.984, 40.6955],
          [-73.9965, 40.698],
          [-74.0105, 40.6925],
          [-74.016, 40.673],
        ]),
        center_lat: 40.6835,
        center_lon: -73.997,
        is_active: true,
      },
      {
        id: 4,
        region_id: 2,
        code: "TER-MIDTOWN",
        name: "Midtown Commons",
        polygon_geojson: polyGeoJson([
          [-73.9755, 40.67],
          [-73.9615, 40.6665],
          [-73.9455, 40.6705],
          [-73.943, 40.6835],
          [-73.9495, 40.696],
          [-73.9645, 40.6975],
          [-73.975, 40.688],
          [-73.9755, 40.67],
        ]),
        center_lat: 40.6815,
        center_lon: -73.9595,
        is_active: true,
      },
    ],
  },
];

const DEMO_PERSONAS: UserProfile[] = [
  { id: 1, username: "admin_hq", full_name: "Elena Vance (HQ Admin)", email: "admin@solstice-demo.example", role: "super_admin", region_id: null, territory_id: null, is_active: true },
  { id: 2, username: "manager_north", full_name: "Marcus Reid (Metro North)", email: "manager.north@solstice-demo.example", role: "regional_manager", region_id: 1, territory_id: null, is_active: true },
  { id: 3, username: "manager_east", full_name: "Sofia Morales (Coastal East)", email: "manager.east@solstice-demo.example", role: "regional_manager", region_id: 2, territory_id: null, is_active: true },
  { id: 4, username: "sup_downtown", full_name: "Daniela Ortega (Downtown Sup)", email: "sup.downtown@solstice-demo.example", role: "supervisor", region_id: 1, territory_id: 1, is_active: true },
  { id: 5, username: "agent_downtown", full_name: "Carlos Mendez (Field Agent)", email: "agent.downtown@solstice-demo.example", role: "field_agent", region_id: 1, territory_id: 1, is_active: true },
  { id: 6, username: "agent_tech", full_name: "Mateo Rivas (Field Agent)", email: "agent.tech@solstice-demo.example", role: "field_agent", region_id: 1, territory_id: 2, is_active: true },
  { id: 7, username: "agent_harbor", full_name: "Lucia Navarro (Field Agent)", email: "agent.harbor@solstice-demo.example", role: "field_agent", region_id: 2, territory_id: 3, is_active: true },
  { id: 8, username: "agent_midtown", full_name: "Valeria Paredes (Field Agent)", email: "agent.midtown@solstice-demo.example", role: "field_agent", region_id: 2, territory_id: 4, is_active: true },
  { id: 9, username: "viewer_north", full_name: "Andres Silva (Read-Only)", email: "viewer.north@solstice-demo.example", role: "viewer", region_id: 1, territory_id: null, is_active: true },
];

function initialOutlets(): Outlet[] {
  return [
    { id: 1, outlet_code: "OUT-1001", business_name: "Aurora Fresh Market", category: "supermarket", address: "114 Mercer Ave", micro_location: "Planta Baja", region_id: 1, territory_id: 1, latitude: 40.712, longitude: -74.006, status: "active", pos_terminal_active: true },
    { id: 2, outlet_code: "OUT-1002", business_name: "Vanguard Pharma Plus", category: "pharmacy", address: "220 Liberty Plaza", micro_location: "Local 102", region_id: 1, territory_id: 1, latitude: 40.718, longitude: -74.001, status: "active", pos_terminal_active: true },
    { id: 3, outlet_code: "OUT-1003", business_name: "Cornerstone Express 24h", category: "convenience", address: "45 Cedar St", micro_location: "Esquina Norte", region_id: 1, territory_id: 1, latitude: 40.725, longitude: -73.992, status: "active", pos_terminal_active: true },
    { id: 4, outlet_code: "OUT-1004", business_name: "Golden Crust Artisan Bakery", category: "bakery", address: "88 Fulton Blvd", micro_location: "Local B", region_id: 1, territory_id: 1, latitude: 40.709, longitude: -73.988, status: "active", pos_terminal_active: false },
    { id: 5, outlet_code: "OUT-1005", business_name: "Apex Hardware & Tools", category: "hardware", address: "310 Canal Way", micro_location: "Galpón 3", region_id: 1, territory_id: 1, latitude: 40.728, longitude: -74.011, status: "active", pos_terminal_active: true },
    { id: 6, outlet_code: "OUT-1006", business_name: "PixelByte Electronics", category: "electronics", address: "19 Hudson Mall", micro_location: "Nivel 2, Local 14", region_id: 1, territory_id: 2, latitude: 40.714, longitude: -73.968, status: "active", pos_terminal_active: true },
    { id: 7, outlet_code: "OUT-1007", business_name: "Verde Organic Grocer", category: "supermarket", address: "410 Foundry Rd", micro_location: "Nave Central", region_id: 1, territory_id: 2, latitude: 40.723, longitude: -73.958, status: "active", pos_terminal_active: true },
    { id: 8, outlet_code: "OUT-1008", business_name: "Pulse Quick Mart", category: "convenience", address: "77 Silicon Sq", micro_location: "Módulo 4", region_id: 1, territory_id: 2, latitude: 40.729, longitude: -73.951, status: "active", pos_terminal_active: true },
    { id: 9, outlet_code: "OUT-1009", business_name: "NovaCare Pharmacy", category: "pharmacy", address: "125 Innovation Pkwy", micro_location: "Lobby Oeste", region_id: 1, territory_id: 2, latitude: 40.707, longitude: -73.972, status: "active", pos_terminal_active: true },
    { id: 10, outlet_code: "OUT-1010", business_name: "Boreal Coffee & Pastries", category: "bakery", address: "52 Tech Walk", micro_location: "Terraza 1", region_id: 1, territory_id: 2, latitude: 40.719, longitude: -73.962, status: "active", pos_terminal_active: false },
    { id: 11, outlet_code: "OUT-1011", business_name: "Pier 9 Marine Supply", category: "hardware", address: "800 Dockside Rd", micro_location: "Almacén A", region_id: 2, territory_id: 3, latitude: 40.682, longitude: -74.008, status: "active", pos_terminal_active: true },
    { id: 12, outlet_code: "OUT-1012", business_name: "Bayfront Supermarket", category: "supermarket", address: "92 Marina Blvd", micro_location: "Tienda Ancla", region_id: 2, territory_id: 3, latitude: 40.691, longitude: -73.997, status: "active", pos_terminal_active: true },
    { id: 13, outlet_code: "OUT-1013", business_name: "Seabreeze Convenience", category: "convenience", address: "14 Lighthouse Ave", micro_location: "Local 2", region_id: 2, territory_id: 3, latitude: 40.674, longitude: -73.991, status: "active", pos_terminal_active: true },
    { id: 14, outlet_code: "OUT-1014", business_name: "Harbor Health Pharmacy", category: "pharmacy", address: "205 Quay St", micro_location: "Suite 105", region_id: 2, territory_id: 3, latitude: 40.686, longitude: -73.986, status: "active", pos_terminal_active: true },
    { id: 15, outlet_code: "OUT-1015", business_name: "Midtown SuperSaver", category: "supermarket", address: "501 Commons Ave", micro_location: "Plaza Norte", region_id: 2, territory_id: 4, latitude: 40.684, longitude: -73.965, status: "active", pos_terminal_active: true },
    { id: 16, outlet_code: "OUT-1016", business_name: "CircuitHub Retail", category: "electronics", address: "620 Grand Mall", micro_location: "Piso 1, Local 8", region_id: 2, territory_id: 4, latitude: 40.693, longitude: -73.955, status: "active", pos_terminal_active: true },
    { id: 17, outlet_code: "OUT-1017", business_name: "Sunrise Corner Bakery", category: "bakery", address: "38 Oak Terrace", micro_location: "Fachada Sur", region_id: 2, territory_id: 4, latitude: 40.676, longitude: -73.969, status: "active", pos_terminal_active: false },
    { id: 18, outlet_code: "OUT-1018", business_name: "MetroFix Hardware", category: "hardware", address: "712 Parkside Rd", micro_location: "Local C", region_id: 2, territory_id: 4, latitude: 40.671, longitude: -73.949, status: "active", pos_terminal_active: true },
  ];
}

export class LocalSimAdapter implements DataAdapter {
  readonly mode: AdapterMode = "demo";
  private user: UserProfile = DEMO_PERSONAS[0];
  private outlets: Outlet[] = initialOutlets();
  private audits: FieldAudit[];
  private triage: TriageItem[];
  private routes: DispatchRoute[];
  private sosAlerts: SosAlert[] = [];
  private tickets: SupportTicket[];
  private listeners = new Set<(event: Record<string, unknown>) => void>();
  private bc: BroadcastChannel | null = null;
  private nextId = 200;

  constructor() {
    const now = new Date().toISOString();
    this.audits = [
      {
        id: 1,
        sync_uuid: "seed-uuid-1003",
        outlet_id: 3,
        outlet_code: "OUT-1003",
        business_name: "Cornerstone Express 24h",
        category: "convenience",
        address: "45 Cedar St",
        micro_location: "Esquina Norte",
        region_id: 1,
        territory_id: 1,
        agent_id: 5,
        route_id: null,
        latitude: 40.7251,
        longitude: -73.9919,
        gps_accuracy_m: 9.2,
        visit_status: "open",
        shelf_share_pct: 78,
        stockout_alert: false,
        pos_active: true,
        signage_compliant: true,
        notes: "Exhibición promocional completa y terminal POS operando.",
        geofence_status: "inside_territory",
        geofence_distance_m: 14.2,
        review_status: "approved",
        captured_at: now,
        synced_at: now,
      },
      {
        id: 2,
        sync_uuid: "seed-uuid-1004",
        outlet_id: 4,
        outlet_code: "OUT-1004",
        business_name: "Golden Crust Artisan Bakery",
        category: "bakery",
        address: "88 Fulton Blvd",
        micro_location: "Local B",
        region_id: 1,
        territory_id: 1,
        agent_id: 5,
        route_id: null,
        latitude: 40.7092,
        longitude: -73.9881,
        gps_accuracy_m: 11.4,
        visit_status: "open",
        shelf_share_pct: 22,
        stockout_alert: true,
        pos_active: false,
        signage_compliant: true,
        notes: "Quiebre crítico de stock en SKU líder; terminal POS sin enlace.",
        geofence_status: "inside_territory",
        geofence_distance_m: 23.5,
        review_status: "quarantined",
        captured_at: now,
        synced_at: now,
      },
      {
        id: 3,
        sync_uuid: "seed-uuid-prospect-901",
        outlet_id: null,
        outlet_code: "PROSPECT-901",
        business_name: "Harbor Pop-Up Kiosk",
        category: "convenience",
        address: "Pier 12 Walkway",
        micro_location: "Módulo Exterior",
        region_id: 2,
        territory_id: 3,
        agent_id: 7,
        route_id: null,
        latitude: 40.688,
        longitude: -73.972,
        gps_accuracy_m: 18.0,
        visit_status: "unmapped_prospect",
        shelf_share_pct: 0,
        stockout_alert: false,
        pos_active: true,
        signage_compliant: false,
        notes: "Nuevo punto comercial detectado fuera del límite oriental de Harbor Strip.",
        geofence_status: "outside_territory",
        geofence_distance_m: 675.0,
        review_status: "quarantined",
        captured_at: now,
        synced_at: now,
      },
    ];

    this.triage = [
      {
        id: 1,
        audit_id: 2,
        region_id: 1,
        territory_id: 1,
        priority: "high",
        alert_type: "stockout_critical",
        status: "open",
        resolution_note: null,
        business_name: "Golden Crust Artisan Bakery",
        outlet_code: "OUT-1004",
        geofence_status: "inside_territory",
        latitude: 40.7092,
        longitude: -73.9881,
        created_at: now,
      },
      {
        id: 2,
        audit_id: 3,
        region_id: 2,
        territory_id: 3,
        priority: "critical",
        alert_type: "out_of_territory",
        status: "open",
        resolution_note: null,
        business_name: "Harbor Pop-Up Kiosk",
        outlet_code: "PROSPECT-901",
        geofence_status: "outside_territory",
        latitude: 40.688,
        longitude: -73.972,
        created_at: now,
      },
    ];

    const today = now.slice(0, 10);
    this.routes = [
      {
        id: 1,
        code: "RT-2026-001",
        title: "Auditoría Matutina — Aurora Fresh Market",
        region_id: 1,
        territory_id: 1,
        agent_id: 5,
        supervisor_id: 4,
        outlet_id: 1,
        scheduled_date: today,
        status: "assigned",
        priority: "high",
        notes: "Validar participación en anaquel y operatividad de POS.",
      },
      {
        id: 2,
        code: "RT-2026-002",
        title: "Verificación POP — Vanguard Pharma Plus",
        region_id: 1,
        territory_id: 1,
        agent_id: 5,
        supervisor_id: 4,
        outlet_id: 2,
        scheduled_date: today,
        status: "assigned",
        priority: "normal",
        notes: "Revisar exhibidor promocional en mostrador.",
      },
      {
        id: 3,
        code: "RT-2026-003",
        title: "Control de Stock — Bayfront Supermarket",
        region_id: 2,
        territory_id: 3,
        agent_id: 7,
        supervisor_id: 3,
        outlet_id: 12,
        scheduled_date: today,
        status: "in_progress",
        priority: "urgent",
        notes: "Auditar reposición en pasillo refrigerado.",
      },
    ];

    this.tickets = [
      {
        id: 1,
        region_id: 1,
        territory_id: 1,
        created_by_id: 5,
        assigned_to_id: 4,
        subject: "Reinicio de terminal POS en Golden Crust",
        status: "claimed",
        resolution_note: null,
        created_at: now,
        messages: [
          {
            id: 1,
            sender_name: "Carlos Mendez (Field Agent)",
            body: "El encargado reporta que el POS quedó congelado durante la auditoría.",
            created_at: now,
          },
        ],
      },
    ];

    if (typeof BroadcastChannel !== "undefined") {
      try {
        this.bc = new BroadcastChannel("quantico_censo_demo_bus");
        this.bc.onmessage = (ev: MessageEvent<Record<string, unknown>>) => {
          this.emitLocal(ev.data);
        };
      } catch (err) {
        console.warn("BroadcastChannel unavailable", err);
        this.bc = null;
      }
    }
  }

  private emit(event: Record<string, unknown>): void {
    this.emitLocal(event);
    if (this.bc) this.bc.postMessage(event);
  }

  private emitLocal(event: Record<string, unknown>): void {
    for (const fn of this.listeners) fn(event);
  }

  private inScope(regionId: number, territoryId: number): boolean {
    if (this.user.role === "super_admin") return true;
    if (this.user.region_id !== regionId) return false;
    if (this.user.territory_id !== null && this.user.territory_id !== territoryId) return false;
    return true;
  }

  private assertWritable(): void {
    if (this.user.role === "viewer") {
      throw new Error("Read-only Viewer role cannot perform write operations (RBAC 403).");
    }
  }

  private findTerritory(territoryId: number): Territory | undefined {
    for (const r of DEMO_REGIONS) {
      const found = r.territories.find((t) => t.id === territoryId);
      if (found) return found;
    }
    return undefined;
  }

  getCurrentUser(): UserProfile {
    return this.user;
  }

  getRealtimeStatus(): RealtimeStatus {
    return "demo";
  }

  listPersonas(): UserProfile[] {
    return DEMO_PERSONAS;
  }

  async listUsers(): Promise<UserProfile[]> {
    if (this.user.role === "super_admin") return DEMO_PERSONAS;
    if (this.user.role === "field_agent") return [this.user];
    return DEMO_PERSONAS.filter(
      (p) =>
        p.region_id === this.user.region_id &&
        (this.user.territory_id === null || p.territory_id === this.user.territory_id),
    );
  }

  async listAgents(): Promise<UserProfile[]> {
    return DEMO_PERSONAS.filter(
      (p) => p.role === "field_agent" && this.inScope(p.region_id ?? 0, p.territory_id ?? 0),
    );
  }

  async switchPersona(username: string): Promise<UserProfile> {
    const found = DEMO_PERSONAS.find((p) => p.username === username);
    if (!found) throw new Error("Unknown demo persona");
    this.user = found;
    this.emitLocal({ event: "PERSONA_CHANGED", role: found.role });
    return found;
  }

  async loginLive(): Promise<UserProfile> {
    throw new Error("Use HttpApiAdapter for live API login.");
  }

  async disconnect(): Promise<void> {
    this.bc?.close();
    this.bc = null;
  }

  async getKpis(): Promise<DashboardKpis> {
    const outlets = this.outlets.filter((o) => this.inScope(o.region_id, o.territory_id));
    const audits = this.audits.filter(
      (a) =>
        this.inScope(a.region_id, a.territory_id) &&
        (this.user.role !== "field_agent" || a.agent_id === this.user.id),
    );
    const triage =
      this.user.role === "field_agent"
        ? []
        : this.triage.filter((t) => t.status === "open" && this.inScope(t.region_id, t.territory_id));
    const sos = this.sosAlerts.filter(
      (s) =>
        s.status === "active" &&
        this.inScope(s.region_id, s.territory_id) &&
        (this.user.role !== "field_agent" || s.agent_id === this.user.id),
    );
    const routes = this.routes.filter(
      (r) =>
        (r.status === "assigned" || r.status === "in_progress") &&
        this.inScope(r.region_id, r.territory_id) &&
        (this.user.role !== "field_agent" || r.agent_id === this.user.id),
    );
    const inside = audits.filter((a) => a.geofence_status === "inside_territory").length;
    const avgShelf = audits.length
      ? Math.round((audits.reduce((acc, a) => acc + a.shelf_share_pct, 0) / audits.length) * 10) / 10
      : 0;
    return {
      total_outlets: outlets.length,
      total_audits: audits.length,
      approved_audits: audits.filter((a) => a.review_status === "approved").length,
      quarantined_audits: audits.filter((a) => a.review_status === "quarantined").length,
      open_triage: triage.length,
      active_sos: sos.length,
      assigned_routes: routes.length,
      stockout_alerts: audits.filter((a) => a.stockout_alert).length,
      avg_shelf_share_pct: avgShelf,
      geofence_compliance_pct: audits.length ? Math.round((inside / audits.length) * 1000) / 10 : 100,
      server_role: "demo-simulator",
    };
  }

  async getMapSnapshot(): Promise<MapSnapshot> {
    const regions = DEMO_REGIONS.filter(
      (r) => this.user.role === "super_admin" || r.id === this.user.region_id,
    ).map((r) => ({
      ...r,
      territories: r.territories.filter(
        (t) => this.user.role === "super_admin" || this.user.territory_id === null || t.id === this.user.territory_id,
      ),
    }));

    const pins: MapPin[] = [];
    for (const o of this.outlets) {
      if (!this.inScope(o.region_id, o.territory_id)) continue;
      pins.push({
        id: o.id,
        kind: "outlet",
        code: o.outlet_code,
        label: o.business_name,
        category: o.category,
        region_id: o.region_id,
        territory_id: o.territory_id,
        latitude: o.latitude,
        longitude: o.longitude,
        status: o.status,
      });
    }
    for (const a of await this.listAudits()) {
      pins.push({
        id: a.id,
        kind: "audit",
        code: a.outlet_code,
        label: a.business_name,
        category: a.category,
        region_id: a.region_id,
        territory_id: a.territory_id,
        latitude: a.latitude,
        longitude: a.longitude,
        status: a.review_status,
        geofence_status: a.geofence_status,
        stockout_alert: a.stockout_alert,
      });
    }
    for (const s of await this.listActiveSos()) {
      pins.push({
        id: s.id,
        kind: "sos",
        code: `SOS-${s.id}`,
        label: `${s.agent_name}: ${s.message}`,
        category: "safety",
        region_id: s.region_id,
        territory_id: s.territory_id,
        latitude: s.latitude,
        longitude: s.longitude,
        status: s.status,
      });
    }
    return { regions, pins, generated_at: new Date().toISOString(), cached: false };
  }

  async listOutlets(): Promise<Outlet[]> {
    return this.outlets.filter((o) => this.inScope(o.region_id, o.territory_id));
  }

  async listAudits(): Promise<FieldAudit[]> {
    return this.audits.filter(
      (a) =>
        this.inScope(a.region_id, a.territory_id) &&
        (this.user.role !== "field_agent" || a.agent_id === this.user.id),
    );
  }

  async listRoutes(): Promise<DispatchRoute[]> {
    return this.routes.filter(
      (r) =>
        this.inScope(r.region_id, r.territory_id) &&
        (this.user.role !== "field_agent" || r.agent_id === this.user.id),
    );
  }

  async listTriage(): Promise<TriageItem[]> {
    if (this.user.role === "field_agent") return [];
    return this.triage.filter((t) => t.status === "open" && this.inScope(t.region_id, t.territory_id));
  }

  async listActiveSos(): Promise<SosAlert[]> {
    return this.sosAlerts.filter(
      (s) =>
        s.status === "active" &&
        this.inScope(s.region_id, s.territory_id) &&
        (this.user.role !== "field_agent" || s.agent_id === this.user.id),
    );
  }

  async listTickets(): Promise<SupportTicket[]> {
    return this.tickets.filter(
      (t) =>
        this.inScope(t.region_id, t.territory_id) &&
        (this.user.role !== "field_agent" || t.created_by_id === this.user.id),
    );
  }

  async listTicketMessages(ticketId: number): Promise<SupportTicket["messages"]> {
    const ticket = this.tickets.find((t) => t.id === ticketId);
    return ticket ? [...ticket.messages] : [];
  }

  async createTicket(subject: string, body: string): Promise<SupportTicket> {
    this.assertWritable();
    const now = new Date().toISOString();
    const ticket: SupportTicket = {
      id: this.nextId++,
      region_id: this.user.region_id ?? 1,
      territory_id: this.user.territory_id ?? 1,
      created_by_id: this.user.id,
      assigned_to_id: null,
      subject,
      status: "open",
      resolution_note: null,
      created_at: now,
      messages: [{ id: this.nextId++, sender_name: this.user.full_name, body, created_at: now }],
    };
    this.tickets.unshift(ticket);
    this.emit({ event: "TICKET_CREATED", ticket_id: ticket.id });
    return ticket;
  }

  async postTicketMessage(ticketId: number, body: string): Promise<void> {
    this.assertWritable();
    const ticket = this.tickets.find((t) => t.id === ticketId);
    if (!ticket || ticket.status === "closed") throw new Error("Ticket closed or not found");
    ticket.messages.push({
      id: this.nextId++,
      sender_name: this.user.full_name,
      body,
      created_at: new Date().toISOString(),
    });
    this.emit({ event: "TICKET_MESSAGE", ticket_id: ticketId });
  }

  async closeTicket(ticketId: number, note: string): Promise<void> {
    this.assertWritable();
    if (this.user.role === "field_agent") throw new Error("Only supervisors can close tickets.");
    const ticket = this.tickets.find((t) => t.id === ticketId);
    if (!ticket) throw new Error("Ticket not found");
    ticket.status = "closed";
    ticket.resolution_note = note;
    this.emit({ event: "TICKET_CLOSED", ticket_id: ticketId });
  }

  async syncBatch(items: FieldAuditInput[]): Promise<BatchSyncResponse> {
    this.assertWritable();
    const results: AuditSyncResultItem[] = [];
    let created = 0;
    let duplicates = 0;
    let conflicts = 0;

    for (const item of items) {
      const existing = this.audits.find((a) => a.sync_uuid === item.sync_uuid);
      if (existing) {
        if (existing.outlet_code === item.outlet_code) {
          duplicates += 1;
          results.push({
            sync_uuid: item.sync_uuid,
            status: "idempotent_duplicate",
            audit_id: existing.id,
            geofence_status: existing.geofence_status,
            review_status: existing.review_status,
            detail: "Already synchronized (idempotent retry).",
          });
        } else {
          conflicts += 1;
          results.push({
            sync_uuid: item.sync_uuid,
            status: "conflict",
            audit_id: null,
            detail: "HTTP 409: sync_uuid collides with another outlet record.",
          });
        }
        continue;
      }

      const outlet = this.outlets.find(
        (o) => o.id === item.outlet_id || o.outlet_code === item.outlet_code,
      );
      const territoryId = outlet ? outlet.territory_id : (this.user.territory_id ?? 1);
      const regionId = outlet ? outlet.region_id : (this.user.region_id ?? 1);
      const territory = this.findTerritory(territoryId);

      let geofenceStatus: FieldAudit["geofence_status"] = "inside_territory";
      let distanceM = 12.0;

      if (territory && !pointInPolygon(item.latitude, item.longitude, territory.polygon_geojson)) {
        geofenceStatus = "outside_territory";
        distanceM = Math.round(haversineMeters(item.latitude, item.longitude, territory.center_lat, territory.center_lon));
      } else if (outlet) {
        distanceM = Math.round(haversineMeters(item.latitude, item.longitude, outlet.latitude, outlet.longitude));
        if (distanceM > 150) geofenceStatus = "gps_drift";
      }

      const needsQuarantine =
        geofenceStatus !== "inside_territory" ||
        item.visit_status === "unmapped_prospect" ||
        item.stockout_alert ||
        !outlet;

      const auditId = this.nextId++;
      const now = new Date().toISOString();
      const record: FieldAudit = {
        ...item,
        id: auditId,
        outlet_id: outlet ? outlet.id : null,
        region_id: regionId,
        territory_id: territoryId,
        agent_id: this.user.id,
        geofence_status: geofenceStatus,
        geofence_distance_m: distanceM,
        review_status: needsQuarantine ? "quarantined" : "approved",
        synced_at: now,
      };
      this.audits.unshift(record);
      created += 1;

      if (item.route_id) {
        const route = this.routes.find((r) => r.id === item.route_id);
        if (route) route.status = "completed";
      }

      if (needsQuarantine) {
        const alertType =
          geofenceStatus === "outside_territory"
            ? "out_of_territory"
            : geofenceStatus === "gps_drift"
              ? "gps_drift"
              : item.stockout_alert
                ? "stockout_critical"
                : "unmapped_prospect";
        const priority = geofenceStatus === "outside_territory" ? "critical" : "high";
        this.triage.unshift({
          id: this.nextId++,
          audit_id: auditId,
          region_id: regionId,
          territory_id: territoryId,
          priority,
          alert_type: alertType,
          status: "open",
          resolution_note: null,
          business_name: record.business_name,
          outlet_code: record.outlet_code,
          geofence_status: geofenceStatus,
          latitude: record.latitude,
          longitude: record.longitude,
          created_at: now,
        });
      }

      results.push({
        sync_uuid: item.sync_uuid,
        status: "created",
        audit_id: auditId,
        geofence_status: geofenceStatus,
        review_status: record.review_status,
        detail: "Synchronized.",
      });
    }

    this.emit({ event: "AUDIT_SYNCED", created });
    return { processed: results.length, created, duplicates, conflicts, results };
  }

  async resolveTriage(
    triageId: number,
    decision: "approved" | "rejected" | "rework_requested",
    note: string,
  ): Promise<TriageItem> {
    this.assertWritable();
    if (this.user.role === "field_agent") {
      throw new Error("Field agents cannot resolve triage items (RBAC 403).");
    }
    const item = this.triage.find((t) => t.id === triageId);
    if (!item) throw new Error("Triage item not found");
    item.status = decision;
    item.resolution_note = note;
    const audit = this.audits.find((a) => a.id === item.audit_id);
    if (audit) audit.review_status = decision;
    this.emit({ event: "TRIAGE_RESOLVED", triage_id: triageId, decision });
    return item;
  }

  async assignRoute(payload: {
    title: string;
    agent_id: number;
    outlet_id: number | null;
    priority: "normal" | "high" | "urgent";
    notes: string;
  }): Promise<DispatchRoute> {
    this.assertWritable();
    if (this.user.role === "field_agent") {
      throw new Error("Field agents cannot dispatch routes (RBAC 403).");
    }
    const agent = DEMO_PERSONAS.find((p) => p.id === payload.agent_id && p.role === "field_agent");
    const outlet = this.outlets.find((o) => o.id === payload.outlet_id);
    const route: DispatchRoute = {
      id: this.nextId++,
      code: `RT-2026-${String(this.nextId).slice(-3)}`,
      title: payload.title,
      region_id: agent?.region_id ?? outlet?.region_id ?? this.user.region_id ?? 1,
      territory_id: agent?.territory_id ?? outlet?.territory_id ?? this.user.territory_id ?? 1,
      agent_id: payload.agent_id,
      supervisor_id: this.user.id,
      outlet_id: payload.outlet_id,
      scheduled_date: new Date().toISOString().slice(0, 10),
      status: "assigned",
      priority: payload.priority,
      notes: payload.notes,
    };
    this.routes.unshift(route);
    this.emit({ event: "ROUTE_ASSIGNED", route_id: route.id });
    return route;
  }

  async triggerSos(payload: {
    latitude: number;
    longitude: number;
    gps_fallback: boolean;
    message: string;
  }): Promise<SosAlert> {
    this.assertWritable();
    const alert: SosAlert = {
      id: this.nextId++,
      region_id: this.user.region_id ?? 1,
      territory_id: this.user.territory_id ?? 1,
      agent_id: this.user.id,
      agent_name: this.user.full_name,
      latitude: payload.latitude,
      longitude: payload.longitude,
      gps_fallback: payload.gps_fallback,
      message: payload.message,
      status: "active",
      resolution_note: null,
      created_at: new Date().toISOString(),
    };
    this.sosAlerts.unshift(alert);
    this.emit({ event: "SOS_TRIGGERED", alert_id: alert.id });
    return alert;
  }

  async resolveSos(alertId: number, note: string): Promise<SosAlert> {
    this.assertWritable();
    if (this.user.role === "field_agent") {
      throw new Error("Only supervisors or managers can dispatch-resolve SOS.");
    }
    const alert = this.sosAlerts.find((s) => s.id === alertId);
    if (!alert) throw new Error("SOS alert not found");
    alert.status = "resolved";
    alert.resolution_note = note;
    this.emit({ event: "SOS_RESOLVED", alert_id: alertId });
    return alert;
  }

  subscribeEvents(onEvent: (event: Record<string, unknown>) => void): () => void {
    this.listeners.add(onEvent);
    return () => this.listeners.delete(onEvent);
  }
}

export class HttpApiAdapter implements DataAdapter {
  readonly mode: AdapterMode = "live";
  private accessToken = "";
  private user: UserProfile = DEMO_PERSONAS[0];
  private ws: WebSocket | null = null;
  private pingTimer: number | null = null;
  private reconnectTimer: number | null = null;
  private reconnectAttempt = 0;
  private manualDisconnect = false;
  private realtimeStatus: RealtimeStatus = "offline";
  private listeners = new Set<(event: Record<string, unknown>) => void>();

  private emit(event: Record<string, unknown>): void {
    for (const fn of this.listeners) fn(event);
  }

  private clearTimers(): void {
    if (this.pingTimer !== null) {
      window.clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (this.accessToken) headers["Authorization"] = `Bearer ${this.accessToken}`;
    const res = await fetch(path, {
      ...init,
      credentials: "include",
      headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) },
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = (await res.json()) as { detail?: string };
        if (body.detail) detail = body.detail;
      } catch {
        detail = `HTTP ${res.status}`;
      }
      throw new Error(detail);
    }
    return (await res.json()) as T;
  }

  getCurrentUser(): UserProfile {
    return this.user;
  }

  getRealtimeStatus(): RealtimeStatus {
    return this.realtimeStatus;
  }

  listPersonas(): UserProfile[] {
    return DEMO_PERSONAS;
  }

  async listUsers(): Promise<UserProfile[]> {
    if (this.user.role === "field_agent" || this.user.role === "viewer") {
      return [this.user];
    }
    return this.request<UserProfile[]>("/api/v1/users?limit=100");
  }

  async listAgents(): Promise<UserProfile[]> {
    if (this.user.role === "field_agent" || this.user.role === "viewer") return [];
    const users = await this.request<UserProfile[]>("/api/v1/users?limit=100");
    return users.filter((u) => u.role === "field_agent" && u.is_active);
  }

  async switchPersona(username: string): Promise<UserProfile> {
    return this.loginLive(username, "QuanticoDemo2026!");
  }

  async loginLive(username: string, password: string): Promise<UserProfile> {
    this.manualDisconnect = false;
    this.reconnectAttempt = 0;
    const data = await this.request<{ access_token: string; user: UserProfile }>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    this.accessToken = data.access_token;
    this.user = data.user;
    await this.connectWebSocket();
    return this.user;
  }

  async disconnect(): Promise<void> {
    this.manualDisconnect = true;
    this.clearTimers();
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.onerror = null;
      this.ws.close();
      this.ws = null;
    }
    this.realtimeStatus = "offline";
    if (this.accessToken) {
      try {
        await this.request<{ status: string }>("/api/v1/auth/logout", { method: "POST" });
      } catch (err) {
        console.warn("Live logout cleanup failed", err);
      }
      this.accessToken = "";
    }
  }

  private scheduleReconnect(): void {
    if (this.manualDisconnect || !this.accessToken || this.reconnectAttempt >= 5) {
      this.realtimeStatus = "offline";
      this.emit({ event: "WS_STATUS_CHANGED", status: this.realtimeStatus });
      return;
    }
    const delayMs = Math.min(12_000, 1_500 * 2 ** this.reconnectAttempt);
    this.reconnectAttempt += 1;
    this.realtimeStatus = "reconnecting";
    this.emit({ event: "WS_STATUS_CHANGED", status: this.realtimeStatus });
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      void this.connectWebSocket();
    }, delayMs);
  }

  private async connectWebSocket(): Promise<void> {
    this.clearTimers();
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.onerror = null;
      this.ws.close();
      this.ws = null;
    }
    if (!this.accessToken || this.manualDisconnect) return;
    try {
      const ticketRes = await this.request<{ ticket: string }>("/api/v1/auth/ws-ticket", { method: "POST" });
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/api/v1/ws/alerts?ticket=${encodeURIComponent(ticketRes.ticket)}`);
      ws.onmessage = (ev: MessageEvent<string>) => {
        try {
          const parsed = JSON.parse(ev.data) as Record<string, unknown>;
          this.emit(parsed);
        } catch (err) {
          console.warn("Malformed WebSocket JSON frame ignored", err);
        }
      };
      ws.onopen = () => {
        this.reconnectAttempt = 0;
        this.realtimeStatus = "connected";
        this.emit({ event: "WS_STATUS_CHANGED", status: this.realtimeStatus });
        this.pingTimer = window.setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send("ping");
        }, 25_000);
      };
      ws.onerror = () => {
        if (this.pingTimer !== null) {
          window.clearInterval(this.pingTimer);
          this.pingTimer = null;
        }
      };
      ws.onclose = (ev: CloseEvent) => {
        if (this.pingTimer !== null) {
          window.clearInterval(this.pingTimer);
          this.pingTimer = null;
        }
        if (this.ws === ws) this.ws = null;
        if (this.manualDisconnect) return;
        if (ev.code === 4401) {
          this.accessToken = "";
          this.realtimeStatus = "revoked";
          this.emit({ event: "SESSION_REVOKED" });
          return;
        }
        this.scheduleReconnect();
      };
      this.ws = ws;
    } catch (err) {
      console.warn("WebSocket handshake unavailable", err);
      this.ws = null;
      this.scheduleReconnect();
    }
  }

  getKpis(): Promise<DashboardKpis> {
    return this.request<DashboardKpis>("/api/v1/hierarchy/dashboard/kpis");
  }

  getMapSnapshot(): Promise<MapSnapshot> {
    return this.request<MapSnapshot>("/api/v1/audits/map");
  }

  listOutlets(): Promise<Outlet[]> {
    return this.request<Outlet[]>("/api/v1/outlets?limit=100");
  }

  listAudits(): Promise<FieldAudit[]> {
    return this.request<FieldAudit[]>("/api/v1/audits?limit=100");
  }

  listRoutes(): Promise<DispatchRoute[]> {
    return this.request<DispatchRoute[]>("/api/v1/dispatch?limit=50");
  }

  async listTriage(): Promise<TriageItem[]> {
    if (this.user.role === "field_agent") return [];
    const data = await this.request<{ items: TriageItem[] }>("/api/v1/triage?status=open&limit=50");
    return data.items;
  }

  listActiveSos(): Promise<SosAlert[]> {
    return this.request<SosAlert[]>("/api/v1/sos/active");
  }

  async listTickets(): Promise<SupportTicket[]> {
    const rows = await this.request<Array<Omit<SupportTicket, "messages">>>("/api/v1/tickets?limit=25");
    return rows.map((r) => ({ ...r, messages: [] }));
  }

  listTicketMessages(ticketId: number): Promise<SupportTicket["messages"]> {
    return this.request<SupportTicket["messages"]>(`/api/v1/tickets/${ticketId}/messages`);
  }

  async createTicket(subject: string, body: string): Promise<SupportTicket> {
    const created = await this.request<Omit<SupportTicket, "messages">>("/api/v1/tickets", {
      method: "POST",
      body: JSON.stringify({ subject, body }),
    });
    const messages = await this.request<SupportTicket["messages"]>(`/api/v1/tickets/${created.id}/messages`);
    return { ...created, messages };
  }

  async postTicketMessage(ticketId: number, body: string): Promise<void> {
    await this.request(`/api/v1/tickets/${ticketId}/messages`, {
      method: "POST",
      body: JSON.stringify({ body }),
    });
  }

  async closeTicket(ticketId: number, note: string): Promise<void> {
    await this.request(`/api/v1/tickets/${ticketId}/close`, {
      method: "POST",
      body: JSON.stringify({ resolution_note: note }),
    });
  }

  syncBatch(items: FieldAuditInput[]): Promise<BatchSyncResponse> {
    return this.request<BatchSyncResponse>("/api/v1/audits/sync", {
      method: "POST",
      body: JSON.stringify({ items }),
    });
  }

  resolveTriage(
    triageId: number,
    decision: "approved" | "rejected" | "rework_requested",
    note: string,
  ): Promise<TriageItem> {
    return this.request<TriageItem>(`/api/v1/triage/${triageId}/resolve`, {
      method: "POST",
      body: JSON.stringify({ decision, resolution_note: note }),
    });
  }

  assignRoute(payload: {
    title: string;
    agent_id: number;
    outlet_id: number | null;
    priority: "normal" | "high" | "urgent";
    notes: string;
  }): Promise<DispatchRoute> {
    return this.request<DispatchRoute>("/api/v1/dispatch", {
      method: "POST",
      body: JSON.stringify({
        ...payload,
        scheduled_date: new Date().toISOString().slice(0, 10),
      }),
    });
  }

  triggerSos(payload: {
    latitude: number;
    longitude: number;
    gps_fallback: boolean;
    message: string;
  }): Promise<SosAlert> {
    return this.request<SosAlert>("/api/v1/sos/trigger", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  resolveSos(alertId: number, note: string): Promise<SosAlert> {
    return this.request<SosAlert>(`/api/v1/sos/${alertId}/resolve`, {
      method: "POST",
      body: JSON.stringify({ resolution_note: note }),
    });
  }

  subscribeEvents(onEvent: (event: Record<string, unknown>) => void): () => void {
    this.listeners.add(onEvent);
    return () => this.listeners.delete(onEvent);
  }
}
