/**
 * Mobile Field Agent PWA View (Quantico Censo light mobile card layout):
 *  - Sticky Quantico Blue mobile header with live ONLINE/OFFLINE toggle & Dashboard return
 *  - 4-tab bottom bar (Census · Routes · Encrypted Vault · SOS Safety)
 *  - Partitioned IndexedDB AES-GCM-256 vault, chunked <=25 sync with mutex, per-entry try/catch
 *  - Safe 3000ms Long-Press SOS with full timer cleanup on render()/blur/pointerup/pointercancel/pointerleave
 */

import { clearNode, el, formatShortTime } from "../shared/dom";
import { t } from "../shared/i18n";
import { cryptoVault } from "../shared/cryptoVault";
import { deleteVaultEntry, listVaultEntries, putVaultEntry } from "../shared/indexedDb";
import type {
  DataAdapter,
  DispatchRoute,
  FieldAuditInput,
  Outlet,
  VaultEncryptedEntry,
} from "../shared/types";

type PwaTab = "capture" | "routes" | "vault" | "safety";
const MAX_SYNC_CHUNK = 25;

export interface FieldPwaCallbacks {
  getAdapter: () => DataAdapter;
  getUserScope: () => string;
  isOffline: () => boolean;
  onToggleOffline: () => void;
  onBackToCommand: () => void;
  onNotify: (message: string, tone?: "info" | "ok" | "warn" | "danger") => void;
  onStateChanged: () => Promise<void>;
}

export class FieldPwaView {
  private container: HTMLElement;
  private cb: FieldPwaCallbacks;
  private activeTab: PwaTab = "capture";
  private outlets: Outlet[] = [];
  private routes: DispatchRoute[] = [];
  private vaultEntries: VaultEncryptedEntry[] = [];
  private syncingInFlight = false;

  // Controlled form state
  private selectedOutletId: number | null = 1;
  private selectedRouteId: number | null = null;
  private outletCode = "OUT-1001";
  private businessName = "Aurora Fresh Market";
  private category: FieldAuditInput["category"] = "supermarket";
  private address = "114 Mercer Ave";
  private microLocation = "Planta Baja";
  private latitude = 40.7121;
  private longitude = -74.0059;
  private visitStatus: FieldAuditInput["visit_status"] = "open";
  private shelfShare = 74;
  private stockoutAlert = false;
  private posActive = true;
  private signageCompliant = true;
  private notes = "";
  private forcedSyncUuid: string | null = null;

  private sosHoldTimer: number | null = null;
  private windowCancelHandler: (() => void) | null = null;
  private sosBtnRef: HTMLButtonElement | null = null;

  constructor(container: HTMLElement, cb: FieldPwaCallbacks) {
    this.container = container;
    this.cb = cb;
  }

  prefillCoordinates(lat: number, lon: number): void {
    this.applyOutlet(null);
    this.latitude = Number(lat.toFixed(6));
    this.longitude = Number(lon.toFixed(6));
    this.activeTab = "capture";
    this.render();
  }

  async refreshData(shared?: { outlets: Outlet[]; routes: DispatchRoute[] }): Promise<void> {
    const adapter = this.cb.getAdapter();
    const scope = this.cb.getUserScope();
    const [outlets, routes, vault] = await Promise.all([
      shared ? Promise.resolve(shared.outlets) : adapter.listOutlets(),
      shared ? Promise.resolve(shared.routes) : adapter.listRoutes(),
      listVaultEntries(scope),
    ]);
    this.outlets = outlets;
    this.routes = routes;
    this.vaultEntries = vault;
    if (this.selectedOutletId !== null && !this.outlets.some((o) => o.id === this.selectedOutletId)) {
      this.applyOutlet(this.outlets[0] ?? null);
    }
    this.render();
  }

  private applyOutlet(outlet: Outlet | null): void {
    this.forcedSyncUuid = null;
    if (!outlet) {
      this.selectedOutletId = null;
      this.outletCode = `PROSPECT-${Math.floor(100 + Math.random() * 899)}`;
      this.businessName = "Nuevo Comercio Detectado";
      this.category = "convenience";
      this.address = "Av. Principal Comercial";
      this.microLocation = "Local Esquina";
      this.latitude = 40.7185;
      this.longitude = -74.0005;
      this.visitStatus = "unmapped_prospect";
      return;
    }
    this.selectedOutletId = outlet.id;
    this.outletCode = outlet.outlet_code;
    this.businessName = outlet.business_name;
    this.category = outlet.category as FieldAuditInput["category"];
    this.address = outlet.address;
    this.microLocation = outlet.micro_location;
    this.latitude = outlet.latitude;
    this.longitude = outlet.longitude;
    this.visitStatus = "open";
  }

  private clearSosTimer(): void {
    if (this.sosHoldTimer !== null) {
      window.clearInterval(this.sosHoldTimer);
      this.sosHoldTimer = null;
    }
    if (this.windowCancelHandler) {
      window.removeEventListener("pointerup", this.windowCancelHandler);
      window.removeEventListener("pointercancel", this.windowCancelHandler);
      window.removeEventListener("blur", this.windowCancelHandler);
      if (this.sosBtnRef) {
        this.sosBtnRef.removeEventListener("pointerleave", this.windowCancelHandler);
      }
      this.windowCancelHandler = null;
      this.sosBtnRef = null;
    }
  }

  private async handleSaveAudit(): Promise<void> {
    const adapter = this.cb.getAdapter();
    if (adapter.getCurrentUser().role === "viewer") {
      this.cb.onNotify(t("viewerBlocked"), "warn");
      return;
    }

    const codeClean = this.outletCode.trim().toUpperCase();
    const nameClean = this.businessName.trim();
    const addrClean = this.address.trim();
    if (codeClean.length < 3) {
      this.cb.onNotify(t("validationCode"), "warn");
      return;
    }
    if (nameClean.length < 2) {
      this.cb.onNotify(t("validationBusiness"), "warn");
      return;
    }
    if (addrClean.length < 3) {
      this.cb.onNotify(t("validationAddress"), "warn");
      return;
    }

    try {
      const syncUuid =
        this.forcedSyncUuid ||
        (typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `qc-${Date.now()}-${Math.random().toString(16).slice(2)}`);

      const payload: FieldAuditInput = {
        sync_uuid: syncUuid,
        outlet_id: this.selectedOutletId,
        outlet_code: codeClean,
        business_name: nameClean,
        category: this.category,
        address: addrClean,
        micro_location: this.microLocation.trim(),
        route_id: this.selectedRouteId,
        latitude: this.latitude,
        longitude: this.longitude,
        gps_accuracy_m: 8.5,
        visit_status: this.visitStatus,
        shelf_share_pct: this.shelfShare,
        stockout_alert: this.stockoutAlert,
        pos_active: this.posActive,
        signage_compliant: this.signageCompliant,
        notes: this.notes.trim(),
        captured_at: new Date().toISOString(),
      };

      const encrypted = await cryptoVault.encryptJson(payload);
      const entry: VaultEncryptedEntry = {
        sync_uuid: syncUuid,
        user_scope: this.cb.getUserScope(),
        outlet_code: payload.outlet_code,
        business_name: payload.business_name,
        iv_b64: encrypted.ivB64,
        ciphertext_b64: encrypted.ciphertextB64,
        status: "pending",
        retry_count: 0,
        last_error: "",
        captured_at: payload.captured_at,
      };
      await putVaultEntry(entry);
      this.forcedSyncUuid = null;
      this.selectedRouteId = null;
      this.notes = "";

      if (this.cb.isOffline()) {
        this.vaultEntries = await listVaultEntries(this.cb.getUserScope());
        this.activeTab = "vault";
        this.cb.onNotify(t("toastSavedOffline"), "info");
        this.render();
        return;
      }

      await this.handleSyncVault();
    } catch (err) {
      this.cb.onNotify(err instanceof Error ? err.message : "Vault encryption failed", "danger");
    }
  }

  async handleSyncVault(): Promise<void> {
    if (this.syncingInFlight) return;
    if (this.cb.isOffline()) {
      this.cb.onNotify(t("pwaOffline"), "warn");
      return;
    }
    const adapter = this.cb.getAdapter();
    if (adapter.getCurrentUser().role === "viewer") {
      this.cb.onNotify(t("viewerBlocked"), "warn");
      return;
    }

    this.syncingInFlight = true;
    try {
      const scope = this.cb.getUserScope();
      const entries = await listVaultEntries(scope);
      const pending = entries.filter((e) => e.status === "pending");
      if (pending.length === 0) return;

      const validItems: Array<{ entry: VaultEncryptedEntry; plain: FieldAuditInput }> = [];
      for (const entry of pending) {
        try {
          const plain = await cryptoVault.decryptJson<FieldAuditInput>(entry.iv_b64, entry.ciphertext_b64);
          validItems.push({ entry, plain });
        } catch (err) {
          await putVaultEntry({
            ...entry,
            status: "conflict",
            retry_count: entry.retry_count + 1,
            last_error: err instanceof Error ? `Decrypt error: ${err.message}` : "Vault key mismatch",
          });
        }
      }

      let totalCreated = 0;
      let totalConflicts = 0;

      for (let i = 0; i < validItems.length; i += MAX_SYNC_CHUNK) {
        const chunk = validItems.slice(i, i + MAX_SYNC_CHUNK);
        const res = await adapter.syncBatch(chunk.map((c) => c.plain));
        totalCreated += res.created;
        totalConflicts += res.conflicts;

        for (const item of res.results) {
          const match = chunk.find((c) => c.entry.sync_uuid === item.sync_uuid)?.entry;
          if (!match) continue;
          if (item.status === "created" || item.status === "idempotent_duplicate") {
            await deleteVaultEntry(item.sync_uuid);
          } else {
            await putVaultEntry({
              ...match,
              status: "conflict",
              retry_count: match.retry_count + 1,
              last_error: item.detail,
            });
          }
        }
      }

      if (totalConflicts > 0) {
        this.activeTab = "vault";
        this.cb.onNotify(t("toastConflict"), "warn");
      } else if (totalCreated > 0) {
        this.cb.onNotify(`${t("toastSynced")} (+${totalCreated})`, "ok");
      }
      await this.cb.onStateChanged();
    } catch (err) {
      this.cb.onNotify(err instanceof Error ? err.message : "Sync failed", "danger");
    } finally {
      this.syncingInFlight = false;
    }
  }

  private async handleResolveConflict(entry: VaultEncryptedEntry): Promise<void> {
    try {
      const plain = await cryptoVault.decryptJson<FieldAuditInput>(entry.iv_b64, entry.ciphertext_b64);
      const freshUuid =
        typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `qc-${Date.now()}`;
      const updated: FieldAuditInput = { ...plain, sync_uuid: freshUuid };
      const enc = await cryptoVault.encryptJson(updated);
      await deleteVaultEntry(entry.sync_uuid);
      await putVaultEntry({
        sync_uuid: freshUuid,
        user_scope: entry.user_scope,
        outlet_code: updated.outlet_code,
        business_name: updated.business_name,
        iv_b64: enc.ivB64,
        ciphertext_b64: enc.ciphertextB64,
        status: "pending",
        retry_count: 0,
        last_error: "",
        captured_at: updated.captured_at,
      });
      await this.handleSyncVault();
    } catch (err) {
      this.cb.onNotify(err instanceof Error ? err.message : "Cannot decrypt entry for retry", "danger");
    }
  }

  private startSosHold(btn: HTMLButtonElement, bar: HTMLElement): void {
    const adapter = this.cb.getAdapter();
    if (adapter.getCurrentUser().role === "viewer") {
      this.cb.onNotify(t("viewerBlocked"), "warn");
      return;
    }
    this.clearSosTimer();
    const started = performance.now();
    const durationMs = 3000;
    const labelSpan = btn.querySelector(".sos-text");
    if (labelSpan) labelSpan.textContent = t("sosHolding");

    const cancel = () => {
      this.clearSosTimer();
      bar.style.width = "0%";
      if (labelSpan) labelSpan.textContent = t("sosHoldLabel");
    };
    this.windowCancelHandler = cancel;
    this.sosBtnRef = btn;
    window.addEventListener("pointerup", cancel);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("blur", cancel);
    btn.addEventListener("pointerleave", cancel);

    this.sosHoldTimer = window.setInterval(() => {
      if (!btn.isConnected) {
        cancel();
        return;
      }
      const elapsed = performance.now() - started;
      bar.style.width = `${Math.min(100, (elapsed / durationMs) * 100)}%`;
      if (elapsed >= durationMs) {
        cancel();
        if (typeof navigator.vibrate === "function") {
          navigator.vibrate([120, 60, 120, 60, 240]);
        }
        void this.fireSos();
      }
    }, 40);
  }

  private async fireSos(): Promise<void> {
    try {
      await this.cb.getAdapter().triggerSos({
        latitude: this.latitude,
        longitude: this.longitude,
        gps_fallback: false,
        message: `Asistencia de seguridad solicitada cerca de ${this.businessName} (${this.outletCode})`,
      });
      this.cb.onNotify(t("toastSosTriggered"), "danger");
      await this.cb.onStateChanged();
    } catch (err) {
      this.cb.onNotify(err instanceof Error ? err.message : "SOS failed", "danger");
    }
  }

  render(): void {
    this.clearSosTimer();
    clearNode(this.container);

    const adapter = this.cb.getAdapter();
    const user = adapter.getCurrentUser();
    const offline = this.cb.isOffline();
    const isViewer = user.role === "viewer";

    const header = el("header", { className: "pwa-header" }, [
      el("div", { className: "pwa-header-left" }, [
        el("span", { className: "pwa-q-mark", text: "Q" }),
        el("div", {}, [
          el("h2", { className: "pwa-title", text: t("pwaHeaderTitle") }),
          el("span", { className: "pwa-user-sub", text: `${user.full_name} · ${user.role}` }),
        ]),
      ]),
      el("div", { className: "pwa-header-right" }, [
        el("button", {
          className: `pwa-net-pill ${offline ? "is-offline" : "is-online"}`,
          type: "button",
          text: offline ? `● ${t("pwaOffline")}` : `● ${t("pwaOnline")}`,
          onClick: () => this.cb.onToggleOffline(),
        }),
        el("button", {
          className: "pwa-back-btn",
          type: "button",
          text: `← ${t("pwaBackDashboard")}`,
          onClick: () => this.cb.onBackToCommand(),
        }),
      ]),
    ]);

    const main = el("main", { className: "pwa-main" });
    if (isViewer) {
      main.appendChild(el("div", { className: "alert-warn", text: t("viewerBlocked") }));
    }

    if (this.activeTab === "capture") {
      main.appendChild(this.renderCaptureCard(isViewer));
    } else if (this.activeTab === "routes") {
      main.appendChild(this.renderRoutesCard(isViewer));
    } else if (this.activeTab === "vault") {
      main.appendChild(this.renderVaultCard(isViewer));
    } else {
      main.appendChild(this.renderSafetyCard(isViewer));
    }

    const pendingCount = this.vaultEntries.length;
    const assignedCount = this.routes.filter((r) => r.status === "assigned").length;

    const bottomNav = el("nav", { className: "pwa-bottom-nav" }, [
      this.navBtn("capture", "📍", t("pwaTabCapture")),
      this.navBtn("routes", "🚚", `${t("pwaTabRoutes")} (${assignedCount})`),
      this.navBtn("vault", "🔐", `${t("pwaTabVault")} (${pendingCount})`),
      this.navBtn("safety", "🚨", t("pwaTabSafety")),
    ]);

    this.container.append(el("div", { className: "pwa-frame" }, [header, main, bottomNav]));
  }

  private navBtn(tab: PwaTab, icon: string, label: string): HTMLElement {
    return el(
      "button",
      {
        className: `pwa-nav-btn ${this.activeTab === tab ? "active" : ""}`,
        type: "button",
        onClick: () => {
          this.activeTab = tab;
          this.render();
        },
      },
      [el("span", { className: "pwa-nav-icon", text: icon }), el("span", { text: label })],
    );
  }

  private renderCaptureCard(isViewer: boolean): HTMLElement {
    const adapter = this.cb.getAdapter();
    const selectOutlet = el("select", { className: "field-input" });
    for (const o of this.outlets) {
      const opt = el("option", { value: String(o.id), text: `${o.outlet_code} — ${o.business_name}` });
      if (o.id === this.selectedOutletId) opt.selected = true;
      selectOutlet.appendChild(opt);
    }
    const prospectOpt = el("option", { value: "prospect", text: t("pwaNewProspect") });
    if (this.selectedOutletId === null) prospectOpt.selected = true;
    selectOutlet.appendChild(prospectOpt);
    selectOutlet.addEventListener("change", () => {
      if (selectOutlet.value === "prospect") {
        this.applyOutlet(null);
      } else {
        this.applyOutlet(this.outlets.find((o) => o.id === Number(selectOutlet.value)) ?? null);
      }
      this.render();
    });

    const presets = el("div", { className: "pwa-preset-row" }, [
      el("button", {
        className: "preset-chip ok",
        type: "button",
        text: t("pwaPresetValid"),
        onClick: () => {
          const base = this.outlets.find((o) => o.id === this.selectedOutletId);
          this.latitude = base ? base.latitude + 0.0001 : 40.7185;
          this.longitude = base ? base.longitude - 0.0001 : -74.0005;
          this.forcedSyncUuid = null;
          this.render();
        },
      }),
      el("button", {
        className: "preset-chip warn",
        type: "button",
        text: t("pwaPresetDrift"),
        onClick: () => {
          const base = this.outlets.find((o) => o.id === this.selectedOutletId);
          this.latitude = base ? base.latitude + 0.003 : 40.722;
          this.longitude = base ? base.longitude + 0.003 : -73.992;
          this.forcedSyncUuid = null;
          this.render();
        },
      }),
      el("button", {
        className: "preset-chip danger",
        type: "button",
        text: t("pwaPresetSpoof"),
        onClick: () => {
          this.latitude = 40.768;
          this.longitude = -73.905;
          this.forcedSyncUuid = null;
          this.render();
        },
      }),
      el("button", {
        className: "preset-chip neutral",
        type: "button",
        text: t("pwaPresetConflict"),
        onClick: () => {
          this.selectedOutletId = null;
          this.forcedSyncUuid =
            adapter.mode === "demo" ? "seed-uuid-1003" : "5af0bc0a-b0c4-5124-a0d8-982bc6044b6c";
          this.outletCode = "OUT-CONFLICT";
          this.businessName = "UUID 409 Conflict Test";
          this.address = "45 Cedar St";
          this.render();
        },
      }),
    ]);

    const catSel = el("select", { className: "field-input" }, [
      el("option", { value: "supermarket", text: "Supermercado / Supermarket" }),
      el("option", { value: "pharmacy", text: "Farmacia / Pharmacy" }),
      el("option", { value: "convenience", text: "Conveniencia / Convenience" }),
      el("option", { value: "bakery", text: "Panadería / Bakery" }),
      el("option", { value: "hardware", text: "Ferretería / Hardware" }),
      el("option", { value: "electronics", text: "Electrónica / Electronics" }),
      el("option", { value: "other", text: "Otro / Other" }),
    ]);
    catSel.value = this.category;
    catSel.addEventListener("change", () => {
      this.category = catSel.value as FieldAuditInput["category"];
    });

    const visitSel = el("select", { className: "field-input" }, [
      el("option", { value: "open", text: t("visitOpen") }),
      el("option", { value: "closed", text: t("visitClosed") }),
      el("option", { value: "unmapped_prospect", text: t("visitProspect") }),
    ]);
    visitSel.value = this.visitStatus;
    visitSel.addEventListener("change", () => {
      this.visitStatus = visitSel.value as FieldAuditInput["visit_status"];
    });

    const shelfLabel = el("strong", { text: `${this.shelfShare}%` });
    const shelfRange = el("input", {
      type: "range",
      min: "0",
      max: "100",
      value: String(this.shelfShare),
      className: "pwa-range",
      onInput: (ev) => {
        this.shelfShare = Number((ev.target as HTMLInputElement).value);
        shelfLabel.textContent = `${this.shelfShare}%`;
      },
    });

    return el("div", { className: "pwa-card" }, [
      el("fieldset", { className: "pwa-fieldset" }, [
        el("legend", { text: t("pwaFieldsetStore") }),
        el("label", { className: "field-label" }, [el("span", { text: t("pwaSelectOutlet") }), selectOutlet]),
        el("div", { className: "pwa-row-2" }, [
          el("label", { className: "field-label" }, [
            el("span", { text: t("lblCode") }),
            el("input", {
              className: "field-input",
              value: this.outletCode,
              onInput: (ev) => {
                this.outletCode = (ev.target as HTMLInputElement).value;
              },
            }),
          ]),
          el("label", { className: "field-label" }, [
            el("span", { text: t("lblBusiness") }),
            el("input", {
              className: "field-input",
              value: this.businessName,
              onInput: (ev) => {
                this.businessName = (ev.target as HTMLInputElement).value;
              },
            }),
          ]),
        ]),
        el("div", { className: "pwa-row-2" }, [
          el("label", { className: "field-label" }, [el("span", { text: t("lblCategory") }), catSel]),
          el("label", { className: "field-label" }, [el("span", { text: t("lblVisitStatus") }), visitSel]),
        ]),
        el("label", { className: "field-label" }, [
          el("span", { text: t("lblAddress") }),
          el("input", {
            className: "field-input",
            value: this.address,
            onInput: (ev) => {
              this.address = (ev.target as HTMLInputElement).value;
            },
          }),
        ]),
      ]),
      el("fieldset", { className: "pwa-fieldset" }, [
        el("legend", { text: t("pwaFieldsetGps") }),
        presets,
        el("div", {
          className: "gps-readout",
          text: `GPS: ${this.latitude.toFixed(5)}, ${this.longitude.toFixed(5)} · ±8.5m`,
        }),
      ]),
      el("fieldset", { className: "pwa-fieldset" }, [
        el("legend", { text: t("pwaFieldsetCommercial") }),
        el("div", { className: "range-head" }, [el("span", { text: t("lblShelf") }), shelfLabel]),
        shelfRange,
        el("div", { className: "pwa-checks" }, [
          el("label", { className: "pwa-check" }, [
            el("input", {
              type: "checkbox",
              checked: this.stockoutAlert,
              onChange: (ev) => {
                this.stockoutAlert = (ev.target as HTMLInputElement).checked;
              },
            }),
            el("span", { text: t("lblStockout") }),
          ]),
          el("label", { className: "pwa-check" }, [
            el("input", {
              type: "checkbox",
              checked: this.posActive,
              onChange: (ev) => {
                this.posActive = (ev.target as HTMLInputElement).checked;
              },
            }),
            el("span", { text: t("lblPos") }),
          ]),
        ]),
        el("textarea", {
          className: "field-input",
          rows: 2,
          value: this.notes,
          placeholder: t("notesPlaceholder"),
          onInput: (ev) => {
            this.notes = (ev.target as HTMLTextAreaElement).value;
          },
        }),
      ]),
      el("button", {
        className: "btn-solid-primary full-w",
        type: "button",
        disabled: isViewer,
        text: t("pwaSaveEncrypt"),
        onClick: () => {
          void this.handleSaveAudit();
        },
      }),
    ]);
  }

  private renderRoutesCard(isViewer: boolean): HTMLElement {
    const card = el("div", { className: "pwa-card" });
    if (this.routes.length === 0) {
      card.appendChild(el("p", { className: "muted-text", text: t("noActiveRoutes") }));
      return card;
    }
    for (const r of this.routes) {
      const row = el("div", { className: "list-card" }, [
        el("div", { className: "list-card-head" }, [
          el("span", { className: `pill pill-${r.priority}`, text: `${r.code} · ${r.priority.toUpperCase()}` }),
          el("span", { className: `pill pill-st-${r.status}`, text: r.status.toUpperCase() }),
        ]),
        el("strong", { text: r.title }),
        el("p", { className: "muted-text", text: r.notes || r.scheduled_date }),
      ]);
      if (r.status === "assigned" || r.status === "in_progress") {
        row.appendChild(
          el("button", {
            className: "btn-outline-primary",
            type: "button",
            disabled: isViewer,
            text: `📍 ${t("pwaTabCapture")}`,
            onClick: () => {
              this.applyOutlet(this.outlets.find((o) => o.id === r.outlet_id) ?? null);
              this.selectedRouteId = r.id;
              this.activeTab = "capture";
              this.render();
            },
          }),
        );
      }
      card.appendChild(row);
    }
    return card;
  }

  private renderVaultCard(isViewer: boolean): HTMLElement {
    const card = el("div", { className: "pwa-card" }, [
      el("p", { className: "muted-text", text: t("pwaVaultHint") }),
    ]);

    const pendingCount = this.vaultEntries.filter((e) => e.status === "pending").length;
    if (pendingCount > 0) {
      card.appendChild(
        el("button", {
          className: "btn-solid-primary full-w",
          type: "button",
          disabled: isViewer || this.cb.isOffline(),
          text: `${t("pwaSyncBatch")} (${pendingCount})`,
          onClick: () => {
            void this.handleSyncVault();
          },
        }),
      );
    }

    if (this.vaultEntries.length === 0) {
      card.appendChild(el("p", { className: "muted-text", text: t("pwaVaultEmpty") }));
      return card;
    }

    for (const entry of this.vaultEntries) {
      const item = el("div", { className: "list-card" }, [
        el("div", { className: "list-card-head" }, [
          el("strong", { text: `${entry.outlet_code} — ${entry.business_name}` }),
          el("span", { className: `pill pill-vault-${entry.status}`, text: entry.status.toUpperCase() }),
        ]),
        el("code", {
          className: "cipher-preview",
          text: `UUID: ${entry.sync_uuid}\nIV (12B): ${entry.iv_b64}\nAES-GCM-256: ${entry.ciphertext_b64.slice(0, 72)}...`,
        }),
        el("span", { className: "muted-small", text: formatShortTime(entry.captured_at) }),
      ]);
      if (entry.last_error) {
        item.appendChild(el("div", { className: "alert-danger", text: entry.last_error }));
      }
      if (entry.status === "conflict") {
        item.appendChild(
          el("div", { className: "action-row" }, [
            el("button", {
              className: "btn-outline-primary",
              type: "button",
              text: t("resolveConflict"),
              onClick: () => {
                void this.handleResolveConflict(entry);
              },
            }),
            el("button", {
              className: "btn-outline-danger",
              type: "button",
              text: t("discardEntry"),
              onClick: () => {
                void deleteVaultEntry(entry.sync_uuid).then(() => this.refreshData());
              },
            }),
          ]),
        );
      }
      card.appendChild(item);
    }
    return card;
  }

  private renderSafetyCard(isViewer: boolean): HTMLElement {
    const bar = el("span", { className: "sos-hold-bar" });
    const sosBtn = el(
      "button",
      {
        className: "pwa-sos-btn",
        type: "button",
        disabled: isViewer,
        ariaLabel: t("sosHoldLabel"),
      },
      [bar, el("span", { className: "sos-text", text: t("sosHoldLabel") })],
    );
    sosBtn.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      this.startSosHold(sosBtn, bar);
    });
    sosBtn.addEventListener("contextmenu", (ev) => ev.preventDefault());

    return el("div", { className: "pwa-card safety-card" }, [
      el("h3", { text: t("pwaSosTitle") }),
      el("p", { className: "muted-text", text: t("pwaSosDesc") }),
      el("div", { className: "gps-readout", text: `${t("gpsCurrent")}: ${this.latitude.toFixed(5)}, ${this.longitude.toFixed(5)}` }),
      sosBtn,
    ]);
  }
}
