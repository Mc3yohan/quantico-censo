/**
 * Full-Viewport Operational GIS Command Center (`Quantico Censo`):
 *  - Collapsible Metrics Strip with Region -> Territory Click Drill-Down
 *  - Full-Height 3-Pane Workspace:
 *      Left Collapsible Records & Filters Sidebar (with floating `> Filters` tab)
 *      Center Full-Bleed Map (Streets / Satellite / GIS layer switcher + Manual Census click-to-pin)
 *      Right Slide-In Record Detail & SOS/Audit Panel (structured info-table + live actions)
 *  - Operational Modals for Triage, Route Dispatch, Support Tickets, Analytics & RBAC Users
 */

import { clearNode, el, formatShortTime } from "../shared/dom";
import { t } from "../shared/i18n";
import { TerritoryMap } from "./TerritoryMap";
import type {
  BaseMapStyle,
  DashboardKpis,
  DataAdapter,
  DispatchRoute,
  FieldAudit,
  MapPin,
  MapSnapshot,
  Outlet,
  SosAlert,
  SupportTicket,
  TriageItem,
  UserProfile,
} from "../shared/types";

export type CommandModalKind = "none" | "triage" | "dispatch" | "support" | "stats" | "users";

export interface CommandCenterCallbacks {
  getAdapter: () => DataAdapter;
  onNotify: (message: string, tone?: "info" | "ok" | "warn" | "danger") => void;
  onStateChanged: () => Promise<void>;
  onManualCaptureAt: (lat: number, lon: number) => void;
}

interface UnifiedRecord {
  key: string;
  kind: "audit" | "outlet";
  id: number;
  code: string;
  businessName: string;
  category: string;
  address: string;
  microLocation: string;
  regionId: number;
  territoryId: number;
  latitude: number;
  longitude: number;
  status: string;
  geofenceStatus: string;
  distanceM: number | null;
  shelfShare: number | null;
  posActive: boolean;
  stockout: boolean;
  notes: string;
  triageId: number | null;
  timestamp: string;
}

export class CommandCenterView {
  private container: HTMLElement;
  private cb: CommandCenterCallbacks;

  private map: TerritoryMap | null = null;
  private mapCanvas: HTMLElement | null = null;
  private mountedDom = false;

  // Persistent DOM slots (prevents destroying Leaflet map or stealing filter input focus on refreshes)
  private sosSlot!: HTMLElement;
  private metricsSlot!: HTMLElement;
  private sidebarEl!: HTMLElement;
  private sidebarBody!: HTMLElement;
  private showTabBtn!: HTMLButtonElement;
  private legendBarEl!: HTMLElement;
  private manualBannerSlot!: HTMLElement;
  private layerBarSlot!: HTMLElement;
  private detailPanelEl!: HTMLElement;
  private modalSlot!: HTMLElement;

  // Data state
  private kpis: DashboardKpis | null = null;
  private snapshot: MapSnapshot | null = null;
  private outlets: Outlet[] = [];
  private audits: FieldAudit[] = [];
  private triage: TriageItem[] = [];
  private routes: DispatchRoute[] = [];
  private sosAlerts: SosAlert[] = [];
  private tickets: SupportTicket[] = [];
  private ticketMessages = new Map<number, SupportTicket["messages"]>();
  private agents: UserProfile[] = [];
  private usersCatalog: UserProfile[] = [];
  private lastUserKey = "";

  // UI state
  private metricsExpanded = false;
  private sidebarOpen = window.innerWidth >= 1120;
  private drillRegionId: number | null = null;
  private selectedRecordKey: string | null = null;
  private selectedSosId: number | null = null;
  private manualPinMode = false;
  private activeModal: CommandModalKind = "none";

  // Filters
  private searchText = "";
  private filterCategory = "all";
  private filterStatus = "all";
  private sortMode = "recent";
  private sidebarMounted = false;

  // Dispatch modal state
  private routeTitle = "";
  private routeOutletId: number | null = null;
  private routeAgentId: number | null = null;
  private routePriority: "normal" | "high" | "urgent" = "high";

  // Ticket modal state
  private ticketSubject = "";
  private ticketBody = "";
  private replyDrafts = new Map<number, string>();

  constructor(container: HTMLElement, cb: CommandCenterCallbacks) {
    this.container = container;
    this.cb = cb;
  }

  openModal(kind: CommandModalKind): void {
    this.activeModal = kind;
    this.renderModal();
    if (kind === "support") {
      void this.loadTicketMessages();
    } else if (kind === "users") {
      void this.loadUsersCatalog();
    }
  }

  private async loadTicketMessages(): Promise<void> {
    const adapter = this.cb.getAdapter();
    try {
      const entries = await Promise.all(
        this.tickets.map(async (tk) => [tk.id, await adapter.listTicketMessages(tk.id)] as const),
      );
      for (const [id, msgs] of entries) {
        this.ticketMessages.set(id, msgs);
      }
      if (this.activeModal === "support") this.renderModal();
    } catch (err) {
      this.cb.onNotify(err instanceof Error ? err.message : "Failed loading ticket threads", "warn");
    }
  }

  private async loadUsersCatalog(): Promise<void> {
    try {
      this.usersCatalog = await this.cb.getAdapter().listUsers();
      if (this.activeModal === "users") this.renderModal();
    } catch (err) {
      this.cb.onNotify(err instanceof Error ? err.message : "Failed loading users", "warn");
    }
  }

  getCachedSnapshotData(): { outlets: Outlet[]; routes: DispatchRoute[] } {
    return { outlets: this.outlets, routes: this.routes };
  }

  enableManualCensusMode(): void {
    const user = this.cb.getAdapter().getCurrentUser();
    if (user.role === "viewer") {
      this.cb.onNotify(t("viewerBlocked"), "warn");
      return;
    }
    this.manualPinMode = true;
    this.renderManualBanner();
  }

  private sanitizeCsvText(raw: string): string {
    const safe = /^[=+\-@\t\r]/.test(raw) ? `'${raw}` : raw;
    return `"${safe.replace(/"/g, '""')}"`;
  }

  exportVisibleCsv(): void {
    const rows = this.getFilteredRecords();
    const header = "code,business_name,category,address,status,geofence_status,latitude,longitude\n";
    const body = rows
      .map((r) =>
        [
          this.sanitizeCsvText(r.code),
          this.sanitizeCsvText(r.businessName),
          this.sanitizeCsvText(r.category),
          this.sanitizeCsvText(r.address),
          this.sanitizeCsvText(r.status),
          this.sanitizeCsvText(r.geofenceStatus),
          String(r.latitude),
          String(r.longitude),
        ].join(","),
      )
      .join("\n");
    const blob = new Blob(["\uFEFF" + header + body], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `quantico_censo_export_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async refreshData(): Promise<void> {
    const adapter = this.cb.getAdapter();
    const user = adapter.getCurrentUser();
    const currentUserKey = `${adapter.mode}::${user.username}`;
    const personaChanged = this.lastUserKey !== "" && this.lastUserKey !== currentUserKey;
    this.lastUserKey = currentUserKey;

    const [kpis, snapshot, outlets, audits, triage, routes, sos, tickets, agents] = await Promise.all([
      adapter.getKpis(),
      adapter.getMapSnapshot(),
      adapter.listOutlets(),
      adapter.listAudits(),
      adapter.listTriage(),
      adapter.listRoutes(),
      adapter.listActiveSos(),
      adapter.listTickets(),
      adapter.listAgents(),
    ]);
    this.kpis = kpis;
    this.snapshot = snapshot;
    this.outlets = outlets;
    this.audits = audits;
    this.triage = triage;
    this.routes = routes;
    this.sosAlerts = sos;
    this.tickets = tickets;
    this.agents = agents;

    if (
      personaChanged ||
      (this.drillRegionId !== null && !this.snapshot.regions.some((r) => r.id === this.drillRegionId))
    ) {
      this.drillRegionId = null;
      this.selectedRecordKey = null;
      this.selectedSosId = null;
      this.ticketMessages.clear();
      this.usersCatalog = [];
    }

    if (this.agents.length > 0 && !this.agents.some((a) => a.id === this.routeAgentId)) {
      this.routeAgentId = this.agents[0].id;
    } else if (this.agents.length === 0) {
      this.routeAgentId = null;
    }
    this.syncRouteOutletWithAgent();

    this.ensureMounted();
    this.renderAllSlots();
    if (personaChanged && this.snapshot) {
      this.map?.fitRegion(this.snapshot, null);
    }
    if (this.activeModal === "support") {
      void this.loadTicketMessages();
    } else if (this.activeModal === "users") {
      void this.loadUsersCatalog();
    }
  }

  private getAgentScopedOutlets(): Outlet[] {
    const agent = this.agents.find((a) => a.id === this.routeAgentId);
    if (!agent || agent.territory_id === null) return this.outlets;
    return this.outlets.filter((o) => o.territory_id === agent.territory_id);
  }

  private syncRouteOutletWithAgent(): void {
    const scoped = this.getAgentScopedOutlets();
    if (this.routeOutletId !== null && !scoped.some((o) => o.id === this.routeOutletId)) {
      this.routeOutletId = scoped[0]?.id ?? null;
    }
  }

  getBadges(): { triage: number; tickets: number; sos: number } {
    return {
      triage: this.triage.length,
      tickets: this.tickets.filter((tk) => tk.status !== "closed").length,
      sos: this.sosAlerts.length,
    };
  }

  rerenderLabels(): void {
    if (!this.mountedDom) return;
    this.sidebarMounted = false;
    this.renderAllSlots();
  }

  invalidateMap(): void {
    this.map?.invalidate();
  }

  destroy(): void {
    this.map?.destroy();
    this.map = null;
    this.mountedDom = false;
    this.sidebarMounted = false;
  }

  private ensureMounted(): void {
    if (this.mountedDom && this.container.contains(this.mapCanvas)) return;

    clearNode(this.container);
    this.sosSlot = el("div", { className: "sos-slot" });
    this.metricsSlot = el("section", { className: "metrics-section" });

    this.sidebarBody = el("div", { className: "sidebar-inner" });
    this.sidebarEl = el("aside", { className: `master-sidebar ${this.sidebarOpen ? "" : "collapsed"}` }, [
      this.sidebarBody,
    ]);

    this.showTabBtn = el("button", {
      className: `sidebar-show-tab ${this.sidebarOpen ? "hidden" : ""}`,
      type: "button",
      text: `› ${t("filtersTab")}`,
      onClick: () => {
        this.sidebarOpen = !this.sidebarOpen;
        this.sidebarEl.classList.toggle("collapsed", !this.sidebarOpen);
        this.showTabBtn.classList.toggle("hidden", this.sidebarOpen);
        this.map?.invalidate();
      },
    });

    this.manualBannerSlot = el("div", { className: "manual-banner-slot" });
    this.layerBarSlot = el("div", { className: "map-layer-bar" });
    this.mapCanvas = el("div", { className: "map-canvas" });
    this.legendBarEl = el("div", { className: "map-legend-pill" });

    const mapStage = el("section", { className: "map-stage" }, [
      this.showTabBtn,
      this.manualBannerSlot,
      this.layerBarSlot,
      this.mapCanvas,
      this.legendBarEl,
    ]);

    this.detailPanelEl = el("aside", { className: "detail-drawer hidden" });
    this.modalSlot = el("div", { className: "modal-slot" });

    const workspace = el("div", { className: "qc-workspace" }, [
      this.sidebarEl,
      mapStage,
      this.detailPanelEl,
    ]);

    this.container.append(this.sosSlot, this.metricsSlot, workspace, this.modalSlot);

    this.map = new TerritoryMap(this.mapCanvas, {
      onPinSelect: (pin) => this.handlePinSelected(pin),
      onMapClick: (lat, lon) => {
        if (!this.manualPinMode) return;
        this.manualPinMode = false;
        this.renderManualBanner();
        this.cb.onManualCaptureAt(lat, lon);
      },
    });
    this.mountedDom = true;
    this.sidebarMounted = false;
  }

  private renderAllSlots(): void {
    this.showTabBtn.textContent = `› ${t("filtersTab")}`;
    this.showTabBtn.classList.toggle("hidden", this.sidebarOpen);
    this.renderLegendBar();
    this.renderSosBanner();
    this.renderMetricsStrip();
    this.renderSidebar();
    this.renderLayerBar();
    this.renderManualBanner();
    if (this.snapshot && this.map) {
      this.map.render(this.snapshot, this.drillRegionId);
    }
    this.renderDetailDrawer();
    if (this.activeModal !== "none") {
      const focusedInsideModal =
        document.activeElement instanceof HTMLElement && this.modalSlot.contains(document.activeElement);
      if (!focusedInsideModal) {
        this.renderModal();
      }
    }
  }

  private renderLegendBar(): void {
    clearNode(this.legendBarEl);
    this.legendBarEl.append(
      el("span", { className: "leg-item" }, [el("i", { className: "leg-dot blue" }), t("legendOutlet")]),
      el("span", { className: "leg-item" }, [el("i", { className: "leg-dot green" }), t("legendApproved")]),
      el("span", { className: "leg-item" }, [el("i", { className: "leg-dot orange" }), t("legendQuarantine")]),
      el("span", { className: "leg-item" }, [el("i", { className: "leg-dot red" }), t("legendSos")]),
    );
  }

  private buildUnifiedRecords(): UnifiedRecord[] {
    const records: UnifiedRecord[] = [];
    const auditedOutletIds = new Set<number>();

    for (const a of this.audits) {
      if (a.outlet_id) auditedOutletIds.add(a.outlet_id);
      const triageMatch = this.triage.find((tr) => tr.audit_id === a.id && tr.status === "open");
      records.push({
        key: `audit-${a.id}`,
        kind: "audit",
        id: a.id,
        code: a.outlet_code,
        businessName: a.business_name,
        category: a.category,
        address: a.address,
        microLocation: a.micro_location,
        regionId: a.region_id,
        territoryId: a.territory_id,
        latitude: a.latitude,
        longitude: a.longitude,
        status: a.review_status,
        geofenceStatus: a.geofence_status,
        distanceM: a.geofence_distance_m,
        shelfShare: a.shelf_share_pct,
        posActive: a.pos_active,
        stockout: a.stockout_alert,
        notes: a.notes,
        triageId: triageMatch ? triageMatch.id : null,
        timestamp: a.synced_at,
      });
    }

    for (const o of this.outlets) {
      if (auditedOutletIds.has(o.id)) continue;
      records.push({
        key: `outlet-${o.id}`,
        kind: "outlet",
        id: o.id,
        code: o.outlet_code,
        businessName: o.business_name,
        category: o.category,
        address: o.address,
        microLocation: o.micro_location,
        regionId: o.region_id,
        territoryId: o.territory_id,
        latitude: o.latitude,
        longitude: o.longitude,
        status: "catalog",
        geofenceStatus: "inside_territory",
        distanceM: null,
        shelfShare: null,
        posActive: o.pos_terminal_active,
        stockout: false,
        notes: "",
        triageId: null,
        timestamp: "",
      });
    }
    return records;
  }

  private getFilteredRecords(): UnifiedRecord[] {
    const q = this.searchText.trim().toLowerCase();
    let rows = this.buildUnifiedRecords().filter((r) => {
      if (this.drillRegionId !== null && r.regionId !== this.drillRegionId) return false;
      if (this.filterCategory !== "all" && r.category !== this.filterCategory) return false;
      if (this.filterStatus === "approved" && r.status !== "approved") return false;
      if (this.filterStatus === "quarantined" && r.status !== "quarantined") return false;
      if (this.filterStatus === "catalog" && r.status !== "catalog") return false;
      if (this.filterStatus === "stockout" && !r.stockout) return false;
      if (q) {
        const hay = `${r.code} ${r.businessName} ${r.address} ${r.category}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    if (this.sortMode === "name_asc") {
      rows = rows.sort((a, b) => a.businessName.localeCompare(b.businessName));
    } else if (this.sortMode === "shelf_desc") {
      rows = rows.sort((a, b) => (b.shelfShare ?? -1) - (a.shelfShare ?? -1));
    } else {
      rows = rows.sort((a, b) => b.timestamp.localeCompare(a.timestamp));
    }
    return rows;
  }

  private handlePinSelected(pin: MapPin): void {
    if (pin.kind === "sos") {
      this.selectedRecordKey = null;
      this.selectedSosId = pin.id;
      this.map?.flyTo(pin.latitude, pin.longitude, 16);
      this.renderDetailDrawer();
      return;
    }
    this.selectedSosId = null;
    const targetKey = `${pin.kind}-${pin.id}`;
    const all = this.buildUnifiedRecords();
    const exact = all.find((r) => r.key === targetKey) ?? all.find((r) => r.code === pin.code);
    if (exact) {
      this.selectedRecordKey = exact.key;
      this.renderDetailDrawer();
    }
  }

  private renderSosBanner(): void {
    clearNode(this.sosSlot);
    if (this.sosAlerts.length === 0) return;
    const top = this.sosAlerts[0];
    const user = this.cb.getAdapter().getCurrentUser();
    const canResolve = user.role !== "viewer" && user.role !== "field_agent";

    this.sosSlot.appendChild(
      el("div", { className: "sos-global-banner" }, [
        el("div", { className: "sos-banner-info" }, [
          el("strong", { text: `🚨 ${t("sosActiveBanner")} — ${top.agent_name}` }),
          el("span", { text: `${top.message} (${top.latitude.toFixed(4)}, ${top.longitude.toFixed(4)})` }),
        ]),
        el("div", { className: "action-row" }, [
          el("button", {
            className: "btn-white-sm",
            type: "button",
            text: `📍 ${t("btnCenterMap")}`,
            onClick: () => this.map?.flyTo(top.latitude, top.longitude, 15),
          }),
          el("button", {
            className: "btn-solid-danger-sm",
            type: "button",
            disabled: !canResolve,
            text: t("btnResolveSos"),
            onClick: () => {
              void this.cb
                .getAdapter()
                .resolveSos(top.id, t("sosDispatchNote"))
                .then(async () => {
                  this.cb.onNotify(t("toastSosResolved"), "ok");
                  await this.cb.onStateChanged();
                })
                .catch((err) => this.cb.onNotify(err instanceof Error ? err.message : "Blocked", "warn"));
            },
          }),
        ]),
      ]),
    );
  }

  private renderMetricsStrip(): void {
    clearNode(this.metricsSlot);
    const k = this.kpis;
    const regions = this.snapshot?.regions ?? [];

    const collapsible = el("div", {
      className: `metrics-panel ${this.metricsExpanded ? "" : "is-collapsed"}`,
    });

    if (this.metricsExpanded) {
      const kpiRow = el("div", { className: "qc-kpi-grid" }, [
        this.statCard(t("kpiOutlets"), String(k?.total_outlets ?? 0), "#0062CC"),
        this.statCard(t("kpiAudits"), String(k?.total_audits ?? 0), "#0284C7"),
        this.statCard(t("kpiApproved"), String(k?.approved_audits ?? 0), "#059669"),
        this.statCard(t("kpiTriage"), String(k?.open_triage ?? 0), "#EA580C"),
        this.statCard(t("kpiCompliance"), `${k?.geofence_compliance_pct ?? 100}%`, "#0D9488"),
        this.statCard(t("kpiShelf"), `${k?.avg_shelf_share_pct ?? 0}%`, "#4F46E5"),
      ]);
      collapsible.appendChild(kpiRow);

      // Region drill-down chips
      if (regions.length > 0) {
        const drillRow = el("div", { className: "drill-row" });
        if (this.drillRegionId !== null) {
          drillRow.appendChild(
            el("button", {
              className: "drill-chip active",
              type: "button",
              text: t("backGlobal"),
              onClick: () => {
                this.drillRegionId = null;
                if (this.snapshot) this.map?.fitRegion(this.snapshot, null);
                this.renderAllSlots();
              },
            }),
          );
        }
        for (const r of regions) {
          const count = this.outlets.filter((o) => o.region_id === r.id).length;
          drillRow.appendChild(
            el("button", {
              className: `drill-chip ${this.drillRegionId === r.id ? "active" : ""}`,
              type: "button",
              text: `${r.name} (${r.territories.length} ${t("zonesWord")} · ${count} PDVs)`,
              onClick: () => {
                this.drillRegionId = this.drillRegionId === r.id ? null : r.id;
                if (this.snapshot) this.map?.fitRegion(this.snapshot, this.drillRegionId);
                this.renderAllSlots();
              },
            }),
          );
        }
        collapsible.appendChild(drillRow);
      }
    }

    const toggleBtn = el("button", {
      className: "metrics-toggle-pill",
      type: "button",
      text: this.metricsExpanded ? `▴ ${t("hideMetrics")}` : `▾ ${t("showMetrics")}`,
      onClick: () => {
        this.metricsExpanded = !this.metricsExpanded;
        this.renderMetricsStrip();
        this.map?.invalidate();
      },
    });

    this.metricsSlot.append(collapsible, toggleBtn);
  }

  private statCard(title: string, value: string, accent: string): HTMLElement {
    const card = el("div", { className: "qc-stat-card" }, [
      el("span", { className: "qc-stat-title", text: title }),
      el("strong", { className: "qc-stat-value", text: value }),
    ]);
    card.style.borderTopColor = accent;
    return card;
  }

  private renderLayerBar(): void {
    clearNode(this.layerBarSlot);
    const active = this.map?.getBaseStyle() ?? "streets";
    const styles: Array<{ id: BaseMapStyle; label: string }> = [
      { id: "streets", label: `🗺️ ${t("mapLayerStreets")}` },
      { id: "satellite", label: `🛰️ ${t("mapLayerSatellite")}` },
      { id: "gis", label: `📐 ${t("mapLayerGis")}` },
    ];
    for (const s of styles) {
      this.layerBarSlot.appendChild(
        el("button", {
          className: `layer-btn ${active === s.id ? "active" : ""}`,
          type: "button",
          text: s.label,
          onClick: () => {
            this.map?.setBaseStyle(s.id);
            this.renderLayerBar();
          },
        }),
      );
    }
  }

  private renderManualBanner(): void {
    clearNode(this.manualBannerSlot);
    if (!this.manualPinMode) return;
    this.manualBannerSlot.appendChild(
      el("div", { className: "manual-pin-banner" }, [
        el("span", { text: `📍 ${t("manualPinBanner")}` }),
        el("button", {
          className: "btn-white-sm",
          type: "button",
          text: t("cancel"),
          onClick: () => {
            this.manualPinMode = false;
            this.renderManualBanner();
          },
        }),
      ]),
    );
  }

  private renderSidebar(): void {
    if (this.sidebarMounted) {
      this.renderSidebarListOnly();
      return;
    }
    clearNode(this.sidebarBody);

    const head = el("div", { className: "sidebar-head" }, [
      el("h3", { text: `📋 ${t("recordsTitle")}` }),
      el("button", {
        className: "icon-close-btn",
        type: "button",
        text: "‹",
        title: t("hideSidebar"),
        onClick: () => {
          this.sidebarOpen = false;
          this.sidebarEl.classList.add("collapsed");
          this.showTabBtn.classList.remove("hidden");
          this.map?.invalidate();
        },
      }),
    ]);

    const searchInput = el("input", {
      className: "field-input",
      placeholder: t("searchPlaceholder"),
      value: this.searchText,
      onInput: (ev) => {
        this.searchText = (ev.target as HTMLInputElement).value;
        this.renderSidebarListOnly();
      },
    });

    const catSel = el("select", { className: "field-input" }, [
      el("option", { value: "all", text: t("allCategories") }),
      el("option", { value: "supermarket", text: "Supermercado / Supermarket" }),
      el("option", { value: "pharmacy", text: "Farmacia / Pharmacy" }),
      el("option", { value: "convenience", text: "Conveniencia / Convenience" }),
      el("option", { value: "bakery", text: "Panadería / Bakery" }),
      el("option", { value: "hardware", text: "Ferretería / Hardware" }),
      el("option", { value: "electronics", text: "Electrónica / Electronics" }),
    ]);
    catSel.value = this.filterCategory;
    catSel.addEventListener("change", () => {
      this.filterCategory = catSel.value;
      this.renderSidebarListOnly();
    });

    const stSel = el("select", { className: "field-input" }, [
      el("option", { value: "all", text: t("allStatuses") }),
      el("option", { value: "approved", text: `🟢 ${t("statusApproved")}` }),
      el("option", { value: "quarantined", text: `🟠 ${t("statusQuarantine")}` }),
      el("option", { value: "catalog", text: `🔵 ${t("statusCatalog")}` }),
      el("option", { value: "stockout", text: `⚠️ ${t("statusStockout")}` }),
    ]);
    stSel.value = this.filterStatus;
    stSel.addEventListener("change", () => {
      this.filterStatus = stSel.value;
      this.renderSidebarListOnly();
    });

    const sortSel = el("select", { className: "field-input" }, [
      el("option", { value: "recent", text: t("sortRecent") }),
      el("option", { value: "name_asc", text: t("sortNameAsc") }),
      el("option", { value: "shelf_desc", text: t("sortShelfDesc") }),
    ]);
    sortSel.value = this.sortMode;
    sortSel.addEventListener("change", () => {
      this.sortMode = sortSel.value;
      this.renderSidebarListOnly();
    });

    const filtersWrap = el("div", { className: "sidebar-filters" }, [
      searchInput,
      el("div", { className: "filter-pair" }, [catSel, stSel]),
      sortSel,
    ]);

    const listHost = el("div", { className: "sidebar-list-host", id: "qc-sidebar-list-host" });
    this.sidebarBody.append(head, filtersWrap, listHost);
    this.sidebarMounted = true;
    this.renderSidebarListOnly();
  }

  private renderSidebarListOnly(): void {
    const host = this.sidebarBody.querySelector("#qc-sidebar-list-host");
    if (!host) return;
    clearNode(host);

    const rows = this.getFilteredRecords();
    host.appendChild(el("div", { className: "list-counter", text: `${rows.length} ${t("recordsCount")}` }));

    const ul = el("ul", { className: "record-list" });
    for (const r of rows) {
      const li = el(
        "li",
        {
          className: `record-row ${this.selectedRecordKey === r.key ? "active" : ""}`,
          onClick: () => {
            this.selectedRecordKey = r.key;
            this.map?.flyTo(r.latitude, r.longitude, 15);
            this.renderDetailDrawer();
            this.renderSidebarListOnly();
          },
        },
        [
          el("div", { className: "record-row-top" }, [
            el("span", { className: "record-code", text: r.code }),
            el("span", { className: `st-badge st-${r.status}`, text: r.status.toUpperCase() }),
          ]),
          el("strong", { className: "record-name", text: r.businessName }),
          el("span", { className: "record-addr", text: `${r.category} · ${r.address}` }),
        ],
      );
      ul.appendChild(li);
    }
    host.appendChild(ul);
  }

  private renderDetailDrawer(): void {
    clearNode(this.detailPanelEl);
    if (this.selectedSosId !== null) {
      const sos = this.sosAlerts.find((s) => s.id === this.selectedSosId);
      if (!sos) {
        this.selectedSosId = null;
        this.detailPanelEl.classList.add("hidden");
        this.map?.invalidate();
        return;
      }
      this.detailPanelEl.classList.remove("hidden");
      const user = this.cb.getAdapter().getCurrentUser();
      const canResolve = user.role !== "viewer" && user.role !== "field_agent";
      const header = el("div", { className: "drawer-head" }, [
        el("h3", { text: `🚨 ${t("sosDetailTitle")}` }),
        el("button", {
          className: "icon-close-btn",
          type: "button",
          text: "×",
          onClick: () => {
            this.selectedSosId = null;
            this.renderDetailDrawer();
          },
        }),
      ]);
      const table = el("table", { className: "qc-info-table" }, [
        this.infoRow(t("sosAgentLabel"), sos.agent_name),
        this.infoRow(t("lblStatus"), sos.status.toUpperCase()),
        this.infoRow(t("sosMessageLabel"), sos.message),
        this.infoRow(t("lblCoords"), `${sos.latitude.toFixed(5)}, ${sos.longitude.toFixed(5)}`),
      ]);
      const actions = el("div", { className: "detail-actions" }, [
        el("button", {
          className: "btn-outline-primary",
          type: "button",
          text: `📍 ${t("btnCenterMap")}`,
          onClick: () => this.map?.flyTo(sos.latitude, sos.longitude, 16),
        }),
        el("button", {
          className: "btn-solid-danger-sm",
          type: "button",
          disabled: !canResolve,
          text: t("btnResolveSos"),
          onClick: () => {
            void this.cb
              .getAdapter()
              .resolveSos(sos.id, t("sosDispatchNote"))
              .then(async () => {
                this.selectedSosId = null;
                this.cb.onNotify(t("toastSosResolved"), "ok");
                await this.cb.onStateChanged();
              })
              .catch((err) => this.cb.onNotify(err instanceof Error ? err.message : "Blocked", "warn"));
          },
        }),
      ]);
      this.detailPanelEl.append(header, el("div", { className: "drawer-body" }, [table, actions]));
      this.map?.invalidate();
      return;
    }

    if (!this.selectedRecordKey) {
      this.detailPanelEl.classList.add("hidden");
      this.map?.invalidate();
      return;
    }
    const rec = this.buildUnifiedRecords().find((r) => r.key === this.selectedRecordKey);
    if (!rec) {
      this.detailPanelEl.classList.add("hidden");
      return;
    }

    this.detailPanelEl.classList.remove("hidden");
    const user = this.cb.getAdapter().getCurrentUser();
    const canManage = user.role !== "viewer" && user.role !== "field_agent";

    const header = el("div", { className: "drawer-head" }, [
      el("h3", { text: t("detailTitle") }),
      el("button", {
        className: "icon-close-btn",
        type: "button",
        text: "×",
        onClick: () => {
          this.selectedRecordKey = null;
          this.renderDetailDrawer();
        },
      }),
    ]);

    const summaryCard = el("div", { className: "detail-hero" }, [
      el("div", { className: "list-card-head" }, [
        el("span", { className: "record-code", text: rec.code }),
        el("span", { className: `st-badge st-${rec.status}`, text: rec.status.toUpperCase() }),
      ]),
      el("strong", { className: "detail-hero-name", text: rec.businessName }),
      el("span", { className: "muted-small", text: `${rec.address}${rec.microLocation ? ` · ${rec.microLocation}` : ""}` }),
    ]);

    const table = el("table", { className: "qc-info-table" }, [
      this.infoRow(t("lblCategory"), rec.category),
      this.infoRow(t("lblPos"), rec.posActive ? t("active") : t("inactive")),
      this.infoRow(t("lblShelf"), rec.shelfShare !== null ? `${rec.shelfShare}%` : "—"),
      this.infoRow(t("lblStockout"), rec.stockout ? `⚠️ ${t("yes")}` : t("no")),
      this.infoRow(t("lblGeofence"), rec.geofenceStatus),
      this.infoRow(t("lblDistance"), rec.distanceM !== null ? `${rec.distanceM} m` : "—"),
      this.infoRow(t("lblCoords"), `${rec.latitude.toFixed(5)}, ${rec.longitude.toFixed(5)}`),
    ]);

    const actions = el("div", { className: "detail-actions" });
    if (rec.triageId !== null) {
      const tid = rec.triageId;
      actions.append(
        el("button", {
          className: "btn-solid-ok",
          type: "button",
          disabled: !canManage,
          text: `✓ ${t("btnApprove")}`,
          onClick: () => void this.handleTriage(tid, "approved"),
        }),
        el("button", {
          className: "btn-outline-primary",
          type: "button",
          disabled: !canManage,
          text: t("btnRework"),
          onClick: () => void this.handleTriage(tid, "rework_requested"),
        }),
        el("button", {
          className: "btn-outline-danger",
          type: "button",
          disabled: !canManage,
          text: t("btnReject"),
          onClick: () => void this.handleTriage(tid, "rejected"),
        }),
      );
    }

    actions.append(
      el("button", {
        className: "btn-outline-primary",
        type: "button",
        text: `📍 ${t("btnCenterMap")}`,
        onClick: () => this.map?.flyTo(rec.latitude, rec.longitude, 16),
      }),
      el("button", {
        className: "btn-solid-primary",
        type: "button",
        disabled: !canManage,
        text: `🚚 ${t("btnDispatchHere")}`,
        onClick: () => {
          this.routeTitle = `${t("dispatchVisitPrefix")} — ${rec.businessName}`;
          const outlet = this.outlets.find((o) => o.outlet_code === rec.code);
          const targetTerId = outlet ? outlet.territory_id : rec.territoryId;
          const matchingAgent = this.agents.find((a) => a.territory_id === targetTerId);
          if (matchingAgent) {
            this.routeAgentId = matchingAgent.id;
            this.routeOutletId = outlet ? outlet.id : null;
          } else {
            this.syncRouteOutletWithAgent();
            this.cb.onNotify(t("toastNoAgentInTerritory"), "warn");
          }
          this.activeModal = "dispatch";
          this.renderModal();
        },
      }),
    );

    const content = el("div", { className: "drawer-body" }, [summaryCard, table]);
    if (rec.notes) {
      content.appendChild(el("div", { className: "detail-notes", text: rec.notes }));
    }
    content.appendChild(actions);

    this.detailPanelEl.append(header, content);
    this.map?.invalidate();
  }

  private infoRow(label: string, val: string): HTMLElement {
    return el("tr", {}, [el("th", { text: label }), el("td", { text: val })]);
  }

  private async handleTriage(
    triageId: number,
    decision: "approved" | "rejected" | "rework_requested",
  ): Promise<void> {
    try {
      await this.cb.getAdapter().resolveTriage(triageId, decision, `${t("triageResolvePrefix")}: ${decision}.`);
      this.cb.onNotify(t("toastTriageDone"), "ok");
      await this.cb.onStateChanged();
    } catch (err) {
      this.cb.onNotify(err instanceof Error ? err.message : "Action blocked", "warn");
    }
  }

  private renderModal(): void {
    clearNode(this.modalSlot);
    if (this.activeModal === "none") return;

    const user = this.cb.getAdapter().getCurrentUser();
    const canManage = user.role !== "viewer" && user.role !== "field_agent";

    let title = "";
    let bodyNode: HTMLElement;

    if (this.activeModal === "triage") {
      title = `🚨 ${t("triageModalTitle")}`;
      bodyNode = this.buildTriageModalBody(canManage);
    } else if (this.activeModal === "dispatch") {
      title = `🚚 ${t("dispatchModalTitle")}`;
      bodyNode = this.buildDispatchModalBody(canManage);
    } else if (this.activeModal === "support") {
      title = `🎧 ${t("supportModalTitle")}`;
      bodyNode = this.buildSupportModalBody(user.role !== "viewer", canManage);
    } else if (this.activeModal === "stats") {
      title = `📊 ${t("statsModalTitle")}`;
      bodyNode = this.buildStatsModalBody();
    } else {
      title = `👥 ${t("usersModalTitle")}`;
      bodyNode = this.buildUsersModalBody();
    }

    const close = () => {
      this.activeModal = "none";
      this.renderModal();
    };

    const backdrop = el("div", {
      className: "modal-backdrop",
      onClick: (ev) => {
        if (ev.target === backdrop) close();
      },
    });
    const dialog = el("div", { className: "modal-dialog" }, [
      el("div", { className: "modal-head" }, [
        el("h3", { text: title }),
        el("button", { className: "icon-close-btn", type: "button", text: "×", onClick: close }),
      ]),
      el("div", { className: "modal-body" }, [bodyNode]),
    ]);
    backdrop.appendChild(dialog);
    this.modalSlot.appendChild(backdrop);
  }

  private buildTriageModalBody(canManage: boolean): HTMLElement {
    const wrap = el("div", { className: "stack-sm" });
    if (!canManage) wrap.appendChild(el("div", { className: "alert-warn", text: t("viewerBlocked") }));
    if (this.triage.length === 0) {
      wrap.appendChild(el("p", { className: "muted-text", text: t("triageEmpty") }));
      return wrap;
    }
    for (const item of this.triage) {
      wrap.appendChild(
        el("div", { className: "list-card" }, [
          el("div", { className: "list-card-head" }, [
            el("span", { className: `pill pill-${item.priority}`, text: `${item.priority.toUpperCase()} · ${item.alert_type}` }),
            el("span", { className: "muted-small", text: formatShortTime(item.created_at) }),
          ]),
          el("strong", { text: `${item.outlet_code} — ${item.business_name}` }),
          el("span", { className: "muted-small", text: `${item.geofence_status} · ${item.latitude.toFixed(5)}, ${item.longitude.toFixed(5)}` }),
          el("div", { className: "action-row" }, [
            el("button", {
              className: "btn-outline-primary",
              type: "button",
              text: `📍 ${t("btnCenterMap")}`,
              onClick: () => {
                this.activeModal = "none";
                this.selectedRecordKey = `audit-${item.audit_id}`;
                this.renderModal();
                this.renderDetailDrawer();
                this.map?.flyTo(item.latitude, item.longitude, 16);
              },
            }),
            el("button", {
              className: "btn-solid-ok",
              type: "button",
              disabled: !canManage,
              text: t("btnApprove"),
              onClick: () => void this.handleTriage(item.id, "approved"),
            }),
            el("button", {
              className: "btn-outline-danger",
              type: "button",
              disabled: !canManage,
              text: t("btnReject"),
              onClick: () => void this.handleTriage(item.id, "rejected"),
            }),
          ]),
        ]),
      );
    }
    return wrap;
  }

  private buildDispatchModalBody(canManage: boolean): HTMLElement {
    this.syncRouteOutletWithAgent();
    const scopedOutlets = this.getAgentScopedOutlets();

    const titleInput = el("input", {
      className: "field-input",
      placeholder: t("defaultRouteTitle"),
      value: this.routeTitle || t("defaultRouteTitle"),
      onInput: (ev) => {
        this.routeTitle = (ev.target as HTMLInputElement).value;
      },
    });
    const agentSel = el("select", { className: "field-input" });
    for (const a of this.agents) {
      const opt = el("option", { value: String(a.id), text: `${a.full_name} (${a.username})` });
      if (a.id === this.routeAgentId) opt.selected = true;
      agentSel.appendChild(opt);
    }
    agentSel.addEventListener("change", () => {
      this.routeAgentId = Number(agentSel.value);
      this.syncRouteOutletWithAgent();
      this.renderModal();
    });

    const outletSel = el("select", { className: "field-input" });
    const patrolOpt = el("option", { value: "", text: t("routePatrolOnly") });
    if (this.routeOutletId === null) patrolOpt.selected = true;
    outletSel.appendChild(patrolOpt);
    for (const o of scopedOutlets) {
      const opt = el("option", { value: String(o.id), text: `${o.outlet_code} — ${o.business_name}` });
      if (o.id === this.routeOutletId) opt.selected = true;
      outletSel.appendChild(opt);
    }
    outletSel.addEventListener("change", () => {
      this.routeOutletId = outletSel.value ? Number(outletSel.value) : null;
    });

    const prioSel = el("select", { className: "field-input" }, [
      el("option", { value: "normal", text: "Normal" }),
      el("option", { value: "high", text: "High" }),
      el("option", { value: "urgent", text: "Urgent" }),
    ]);
    prioSel.value = this.routePriority;
    prioSel.addEventListener("change", () => {
      this.routePriority = prioSel.value as "normal" | "high" | "urgent";
    });

    const form = el("div", { className: "list-card" }, [
      el("label", { className: "field-label" }, [el("span", { text: t("lblRouteTitle") }), titleInput]),
      el("div", { className: "pwa-row-2" }, [
        el("label", { className: "field-label" }, [el("span", { text: t("lblRouteAgent") }), agentSel]),
        el("label", { className: "field-label" }, [el("span", { text: t("lblRoutePriority") }), prioSel]),
      ]),
      el("label", { className: "field-label" }, [el("span", { text: t("lblRouteOutlet") }), outletSel]),
      el("button", {
        className: "btn-solid-primary",
        type: "button",
        disabled: !canManage || this.routeAgentId === null,
        text: t("btnAssignRoute"),
        onClick: () => {
          if (this.routeAgentId === null) return;
          void this.cb
            .getAdapter()
            .assignRoute({
              title: this.routeTitle.trim() || t("defaultRouteTitle"),
              agent_id: this.routeAgentId,
              outlet_id: this.routeOutletId,
              priority: this.routePriority,
              notes: t("dispatchDefaultNote"),
            })
            .then(async () => {
              this.cb.onNotify(t("toastRouteAssigned"), "ok");
              await this.cb.onStateChanged();
            })
            .catch((err) => this.cb.onNotify(err instanceof Error ? err.message : "Dispatch failed", "warn"));
        },
      }),
    ]);

    const wrap = el("div", { className: "stack-sm" }, [form]);
    for (const r of this.routes) {
      wrap.appendChild(
        el("div", { className: "list-card" }, [
          el("div", { className: "list-card-head" }, [
            el("span", { className: `pill pill-${r.priority}`, text: `${r.code} · ${r.priority.toUpperCase()}` }),
            el("span", { className: `pill pill-st-${r.status}`, text: r.status.toUpperCase() }),
          ]),
          el("strong", { text: r.title }),
          el("span", { className: "muted-small", text: `${r.scheduled_date} · ${r.notes}` }),
        ]),
      );
    }
    return wrap;
  }

  private buildSupportModalBody(canWrite: boolean, canClose: boolean): HTMLElement {
    const wrap = el("div", { className: "stack-sm" });
    const subjIn = el("input", {
      className: "field-input",
      placeholder: t("lblTicketSubject"),
      value: this.ticketSubject,
      onInput: (ev) => {
        this.ticketSubject = (ev.target as HTMLInputElement).value;
      },
    });
    const bodyIn = el("input", {
      className: "field-input",
      placeholder: t("lblTicketBody"),
      value: this.ticketBody,
      onInput: (ev) => {
        this.ticketBody = (ev.target as HTMLInputElement).value;
      },
    });
    wrap.appendChild(
      el("div", { className: "list-card" }, [
        subjIn,
        bodyIn,
        el("button", {
          className: "btn-solid-primary",
          type: "button",
          disabled: !canWrite,
          text: t("btnCreateTicket"),
          onClick: () => {
            if (this.ticketSubject.trim().length < 3 || !this.ticketBody.trim()) {
              this.cb.onNotify(t("validationTicket"), "warn");
              return;
            }
            void this.cb
              .getAdapter()
              .createTicket(this.ticketSubject.trim(), this.ticketBody.trim())
              .then(async (created) => {
                this.ticketSubject = "";
                this.ticketBody = "";
                this.ticketMessages.set(created.id, created.messages);
                await this.cb.onStateChanged();
              })
              .catch((err) => this.cb.onNotify(err instanceof Error ? err.message : "Error", "warn"));
          },
        }),
      ]),
    );

    if (this.tickets.length === 0) {
      wrap.appendChild(el("p", { className: "muted-text", text: t("noTickets") }));
      return wrap;
    }

    for (const tk of this.tickets) {
      const threadMsgs = this.ticketMessages.get(tk.id) ?? tk.messages;
      const msgs = el("div", { className: "ticket-thread" });
      for (const m of threadMsgs) {
        msgs.appendChild(
          el("div", { className: "ticket-msg" }, [
            el("strong", { text: `${m.sender_name}: ` }),
            el("span", { text: m.body }),
          ]),
        );
      }
      const replyIn = el("input", {
        className: "field-input",
        placeholder: "...",
        value: this.replyDrafts.get(tk.id) ?? "",
        onInput: (ev) => {
          this.replyDrafts.set(tk.id, (ev.target as HTMLInputElement).value);
        },
      });
      const card = el("div", { className: "list-card" }, [
        el("div", { className: "list-card-head" }, [
          el("strong", { text: `#${tk.id} — ${tk.subject}` }),
          el("span", { className: `pill pill-st-${tk.status}`, text: tk.status.toUpperCase() }),
        ]),
        msgs,
      ]);
      if (tk.status !== "closed") {
        card.appendChild(
          el("div", { className: "action-row" }, [
            replyIn,
            el("button", {
              className: "btn-outline-primary",
              type: "button",
              disabled: !canWrite,
              text: t("btnSendReply"),
              onClick: () => {
                const msg = (this.replyDrafts.get(tk.id) ?? "").trim();
                if (!msg) return;
                void this.cb
                  .getAdapter()
                  .postTicketMessage(tk.id, msg)
                  .then(async () => {
                    this.replyDrafts.delete(tk.id);
                    await this.cb.onStateChanged();
                  })
                  .catch((err) => this.cb.onNotify(err instanceof Error ? err.message : "Reply failed", "warn"));
              },
            }),
            el("button", {
              className: "btn-outline-danger",
              type: "button",
              disabled: !canClose,
              text: t("btnCloseTicket"),
              onClick: () => {
                void this.cb
                  .getAdapter()
                  .closeTicket(tk.id, t("ticketCloseNote"))
                  .then(() => this.cb.onStateChanged())
                  .catch((err) => this.cb.onNotify(err instanceof Error ? err.message : "Close failed", "warn"));
              },
            }),
          ]),
        );
      }
      wrap.appendChild(card);
    }
    return wrap;
  }

  private buildStatsModalBody(): HTMLElement {
    const wrap = el("div", { className: "stack-sm" });
    const regions = this.snapshot?.regions ?? [];
    for (const r of regions) {
      const regOutlets = this.outlets.filter((o) => o.region_id === r.id);
      const regAudits = this.audits.filter((a) => a.region_id === r.id);
      const cov = regOutlets.length ? Math.min(100, Math.round((regAudits.length / regOutlets.length) * 100)) : 0;
      const barFill = el("div", { className: "progress-fill" });
      barFill.style.width = `${cov}%`;
      wrap.appendChild(
        el("div", { className: "list-card" }, [
          el("div", { className: "list-card-head" }, [
            el("strong", { text: `${r.name} (${r.code})` }),
            el("span", { className: "pill", text: `${regAudits.length}/${regOutlets.length} PDVs (${cov}%)` }),
          ]),
          el("div", { className: "progress-track" }, [barFill]),
          el("span", {
            className: "muted-small",
            text: r.territories.map((tr) => tr.name).join(" · "),
          }),
        ]),
      );
    }
    return wrap;
  }

  private buildUsersModalBody(): HTMLElement {
    const wrap = el("div", { className: "stack-sm" });
    const rows =
      this.usersCatalog.length > 0
        ? this.usersCatalog
        : [this.cb.getAdapter().getCurrentUser()];
    for (const p of rows) {
      wrap.appendChild(
        el("div", { className: "list-card" }, [
          el("div", { className: "list-card-head" }, [
            el("strong", { text: p.full_name }),
            el("span", { className: "pill", text: p.role }),
          ]),
          el("span", {
            className: "muted-small",
            text: `${p.username} · ${p.email} · Region=${p.region_id ?? "ALL"} · Territory=${p.territory_id ?? "ALL"}`,
          }),
        ]),
      );
    }
    return wrap;
  }
}
