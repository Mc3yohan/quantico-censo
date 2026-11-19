export type Role = "super_admin" | "regional_manager" | "supervisor" | "field_agent" | "viewer";
export type Lang = "es" | "en";
export type AppSurface = "command" | "pwa";
export type AdapterMode = "demo" | "live";
export type BaseMapStyle = "streets" | "satellite" | "gis";

export interface UserProfile {
  id: number;
  username: string;
  full_name: string;
  email: string;
  role: Role;
  region_id: number | null;
  territory_id: number | null;
  is_active: boolean;
}

export interface Territory {
  id: number;
  region_id: number;
  code: string;
  name: string;
  polygon_geojson: string;
  center_lat: number;
  center_lon: number;
  is_active: boolean;
}

export interface Region {
  id: number;
  code: string;
  name: string;
  country_code: string;
  is_active: boolean;
  territories: Territory[];
}

export interface Outlet {
  id: number;
  outlet_code: string;
  business_name: string;
  category: string;
  address: string;
  micro_location: string;
  region_id: number;
  territory_id: number;
  latitude: number;
  longitude: number;
  status: string;
  pos_terminal_active: boolean;
}

export interface FieldAuditInput {
  sync_uuid: string;
  outlet_id: number | null;
  outlet_code: string;
  business_name: string;
  category: "supermarket" | "pharmacy" | "convenience" | "hardware" | "bakery" | "electronics" | "other";
  address: string;
  micro_location: string;
  route_id: number | null;
  latitude: number;
  longitude: number;
  gps_accuracy_m: number;
  visit_status: "open" | "closed_temp" | "relocated" | "unmapped_prospect";
  shelf_share_pct: number;
  stockout_alert: boolean;
  pos_active: boolean;
  signage_compliant: boolean;
  notes: string;
  captured_at: string;
}

export interface FieldAudit extends FieldAuditInput {
  id: number;
  region_id: number;
  territory_id: number;
  agent_id: number;
  geofence_status: "inside_territory" | "outside_territory" | "gps_drift" | "unknown";
  geofence_distance_m: number | null;
  review_status: "quarantined" | "approved" | "rejected" | "rework_requested";
  synced_at: string;
}

export interface TriageItem {
  id: number;
  audit_id: number;
  region_id: number;
  territory_id: number;
  priority: "critical" | "high" | "medium" | "low";
  alert_type: string;
  status: "open" | "approved" | "rejected" | "rework_requested";
  resolution_note: string | null;
  business_name: string;
  outlet_code: string;
  geofence_status: string;
  latitude: number;
  longitude: number;
  created_at: string;
}

export interface DispatchRoute {
  id: number;
  code: string;
  title: string;
  region_id: number;
  territory_id: number;
  agent_id: number;
  supervisor_id: number;
  outlet_id: number | null;
  scheduled_date: string;
  status: "assigned" | "in_progress" | "completed" | "revoked";
  priority: "normal" | "high" | "urgent";
  notes: string;
}

export interface SosAlert {
  id: number;
  region_id: number;
  territory_id: number;
  agent_id: number;
  agent_name: string;
  latitude: number;
  longitude: number;
  gps_fallback: boolean;
  message: string;
  status: "active" | "resolved" | "cancelled";
  resolution_note: string | null;
  created_at: string;
}

export interface SupportTicket {
  id: number;
  region_id: number;
  territory_id: number;
  created_by_id: number;
  assigned_to_id: number | null;
  subject: string;
  status: "open" | "claimed" | "closed";
  resolution_note: string | null;
  created_at: string;
  messages: Array<{ id: number; sender_name: string; body: string; created_at: string }>;
}

export interface DashboardKpis {
  total_outlets: number;
  total_audits: number;
  approved_audits: number;
  quarantined_audits: number;
  open_triage: number;
  active_sos: number;
  assigned_routes: number;
  stockout_alerts: number;
  avg_shelf_share_pct: number;
  geofence_compliance_pct: number;
  server_role: string;
}

export interface MapPin {
  id: number;
  kind: "outlet" | "audit" | "sos";
  code: string;
  label: string;
  category: string;
  region_id: number;
  territory_id: number;
  latitude: number;
  longitude: number;
  status: string;
  geofence_status?: string | null;
  stockout_alert?: boolean;
}

export interface MapSnapshot {
  regions: Region[];
  pins: MapPin[];
  generated_at: string;
  cached: boolean;
}

export interface AuditSyncResultItem {
  sync_uuid: string;
  status: "created" | "idempotent_duplicate" | "conflict" | "error";
  audit_id?: number | null;
  geofence_status?: string | null;
  review_status?: string | null;
  detail: string;
}

export interface BatchSyncResponse {
  processed: number;
  created: number;
  duplicates: number;
  conflicts: number;
  results: AuditSyncResultItem[];
}

export interface VaultEncryptedEntry {
  sync_uuid: string;
  user_scope: string;
  outlet_code: string;
  business_name: string;
  iv_b64: string;
  ciphertext_b64: string;
  status: "pending" | "syncing" | "conflict" | "synced";
  retry_count: number;
  last_error: string;
  captured_at: string;
}

export type RealtimeStatus = "demo" | "connected" | "reconnecting" | "revoked" | "offline";

export interface DataAdapter {
  readonly mode: AdapterMode;
  getCurrentUser(): UserProfile;
  getRealtimeStatus(): RealtimeStatus;
  listPersonas(): UserProfile[];
  listUsers(): Promise<UserProfile[]>;
  listAgents(): Promise<UserProfile[]>;
  switchPersona(username: string): Promise<UserProfile>;
  loginLive(username: string, password: string): Promise<UserProfile>;
  disconnect(): Promise<void>;
  getKpis(): Promise<DashboardKpis>;
  getMapSnapshot(): Promise<MapSnapshot>;
  listOutlets(): Promise<Outlet[]>;
  listAudits(): Promise<FieldAudit[]>;
  listRoutes(): Promise<DispatchRoute[]>;
  listTriage(): Promise<TriageItem[]>;
  listActiveSos(): Promise<SosAlert[]>;
  listTickets(): Promise<SupportTicket[]>;
  listTicketMessages(ticketId: number): Promise<SupportTicket["messages"]>;
  createTicket(subject: string, body: string): Promise<SupportTicket>;
  postTicketMessage(ticketId: number, body: string): Promise<void>;
  closeTicket(ticketId: number, note: string): Promise<void>;
  syncBatch(items: FieldAuditInput[]): Promise<BatchSyncResponse>;
  resolveTriage(triageId: number, decision: "approved" | "rejected" | "rework_requested", note: string): Promise<TriageItem>;
  assignRoute(payload: { title: string; agent_id: number; outlet_id: number | null; priority: "normal" | "high" | "urgent"; notes: string }): Promise<DispatchRoute>;
  triggerSos(payload: { latitude: number; longitude: number; gps_fallback: boolean; message: string }): Promise<SosAlert>;
  resolveSos(alertId: number, note: string): Promise<SosAlert>;
  subscribeEvents(onEvent: (event: Record<string, unknown>) => void): () => void;
}
