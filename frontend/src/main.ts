/**
 * Quantico Censo — Full-Viewport Operational Command Center & Mobile Field PWA Shell
 */

import "./styles/app.css";
import { clearNode, el } from "./shared/dom";
import { getLang, onLangChange, setLang, t } from "./shared/i18n";
import { cryptoVault } from "./shared/cryptoVault";
import { getOrCreateVaultSalt } from "./shared/indexedDb";
import { HttpApiAdapter, LocalSimAdapter } from "./shared/dataAdapter";
import { CommandCenterView } from "./components/CommandCenterView";
import { FieldPwaView } from "./components/FieldPwaView";
import type { AppSurface, DataAdapter } from "./shared/types";

class QuanticoCensoShell {
  private root: HTMLElement;
  private demoAdapter = new LocalSimAdapter();
  private liveAdapter = new HttpApiAdapter();
  private adapter: DataAdapter = this.demoAdapter;
  private offlineSimulated = false;
  private toolsMenuOpen = false;
  private showLiveModal = false;
  private unsubscribeEvents: (() => void) | null = null;
  private refreshTimer: number | null = null;
  private refreshInFlight = false;
  private refreshPending = false;

  private toastStack: HTMLElement;
  private headerSlot: HTMLElement;
  private commandHost: HTMLElement;
  private pwaHost: HTMLElement;
  private liveModalHost: HTMLElement;
  private commandView: CommandCenterView;
  private pwaView: FieldPwaView;

  constructor(root: HTMLElement) {
    this.root = root;
    this.toastStack = el("div", { className: "toast-stack" });
    document.body.appendChild(this.toastStack);

    this.headerSlot = el("div", { className: "header-slot" });
    this.commandHost = el("div", { className: "command-surface" });
    this.pwaHost = el("div", { className: "pwa-surface hidden" });
    this.liveModalHost = el("div", { className: "live-modal-slot" });

    this.root.append(this.headerSlot, this.commandHost, this.pwaHost, this.liveModalHost);

    this.commandView = new CommandCenterView(this.commandHost, {
      getAdapter: () => this.adapter,
      onNotify: (msg, tone) => this.notify(msg, tone),
      onStateChanged: () => this.refreshAll(),
      onManualCaptureAt: (lat, lon) => {
        this.switchSurface("pwa");
        this.pwaView.prefillCoordinates(lat, lon);
      },
    });

    this.pwaView = new FieldPwaView(this.pwaHost, {
      getAdapter: () => this.adapter,
      getUserScope: () => this.getUserScope(),
      isOffline: () => this.offlineSimulated,
      onToggleOffline: () => this.toggleOffline(),
      onBackToCommand: () => this.switchSurface("command"),
      onNotify: (msg, tone) => this.notify(msg, tone),
      onStateChanged: () => this.refreshAll(),
    });

    onLangChange(() => {
      this.renderHeader();
      this.commandView.rerenderLabels();
      this.pwaView.render();
    });

    document.addEventListener("click", (ev) => {
      if (!this.toolsMenuOpen) return;
      const target = ev.target as HTMLElement | null;
      if (target && !target.closest(".tools-wrap")) {
        this.toolsMenuOpen = false;
        this.renderHeader();
      }
    });

    window.addEventListener("pagehide", () => {
      cryptoVault.dispose();
    });
  }

  private getUserScope(): string {
    return `${this.adapter.mode}::${this.adapter.getCurrentUser().username}`;
  }

  private async unlockVault(passphrase: string): Promise<void> {
    const scope = this.getUserScope();
    const salt = await getOrCreateVaultSalt(scope);
    await cryptoVault.unlock(passphrase, salt);
  }

  async boot(): Promise<void> {
    await this.unlockVault(`quantico-censo-demo-vault::${this.adapter.getCurrentUser().username}`);
    this.bindRealtime();
    await this.refreshAll();
  }

  private scheduleRefresh(): void {
    if (this.refreshTimer !== null) {
      window.clearTimeout(this.refreshTimer);
    }
    this.refreshTimer = window.setTimeout(() => {
      this.refreshTimer = null;
      void this.refreshAll();
    }, 250);
  }

  private bindRealtime(): void {
    if (this.unsubscribeEvents) {
      this.unsubscribeEvents();
      this.unsubscribeEvents = null;
    }
    this.unsubscribeEvents = this.adapter.subscribeEvents((ev) => {
      const eventType = String(ev.event || "");
      if (!eventType || eventType === "CONNECTED" || eventType === "PONG") return;
      if (eventType === "WS_STATUS_CHANGED") {
        this.renderHeader();
        return;
      }
      if (eventType === "SESSION_REVOKED") {
        this.renderHeader();
        this.notify(t("toastSessionRevoked"), "danger");
        return;
      }
      this.scheduleRefresh();
    });
  }

  private switchSurface(next: AppSurface): void {
    if (next === "command") {
      this.headerSlot.classList.remove("hidden");
      this.commandHost.classList.remove("hidden");
      this.pwaHost.classList.add("hidden");
      this.commandView.invalidateMap();
    } else {
      this.headerSlot.classList.add("hidden");
      this.commandHost.classList.add("hidden");
      this.pwaHost.classList.remove("hidden");
      this.pwaView.render();
    }
  }

  private toggleOffline(): void {
    this.offlineSimulated = !this.offlineSimulated;
    this.pwaView.render();
    this.notify(
      this.offlineSimulated ? `● ${t("pwaOffline")}` : `● ${t("pwaOnline")}`,
      this.offlineSimulated ? "warn" : "ok",
    );
  }

  private notify(message: string, tone: "info" | "ok" | "warn" | "danger" = "info"): void {
    const item = el("div", { className: `toast ${tone}`, text: message });
    this.toastStack.appendChild(item);
    window.setTimeout(() => item.remove(), 3600);
  }

  async refreshAll(): Promise<void> {
    if (this.refreshInFlight) {
      this.refreshPending = true;
      return;
    }
    this.refreshInFlight = true;
    try {
      await this.commandView.refreshData();
      await this.pwaView.refreshData(this.commandView.getCachedSnapshotData());
      this.renderHeader();
      this.renderLiveModal();
    } catch (err) {
      this.notify(err instanceof Error ? err.message : "Failed to load data", "danger");
    } finally {
      this.refreshInFlight = false;
      if (this.refreshPending) {
        this.refreshPending = false;
        this.scheduleRefresh();
      }
    }
  }

  private renderHeader(): void {
    clearNode(this.headerSlot);
    const user = this.adapter.getCurrentUser();
    const badges = this.commandView.getBadges();
    const lang = getLang();
    const wsStatus = this.adapter.getRealtimeStatus();

    const wsChip =
      this.adapter.mode === "live"
        ? el("span", {
            className: `ws-chip ws-${wsStatus}`,
            text:
              wsStatus === "connected"
                ? `● ${t("wsLive")}`
                : wsStatus === "reconnecting"
                  ? `◌ ${t("wsReconnecting")}`
                  : wsStatus === "revoked"
                    ? `✕ ${t("wsRevoked")}`
                    : `○ ${t("wsOffline")}`,
          })
        : null;

    const qLogo = el("div", { className: "q-emblem" }, [el("span", { text: "Q" })]);
    const brand = el("div", { className: "cmd-brand" }, [
      qLogo,
      el("div", { className: "cmd-brand-text" }, [
        el("div", { className: "cmd-brand-row" }, [
          el("h1", { className: "cmd-title", text: t("brandName") }),
          el("span", {
            className: `env-chip ${this.adapter.mode === "live" ? "live" : ""}`,
            text: this.adapter.mode === "live" ? t("liveBadge") : t("demoBadge"),
          }),
          wsChip,
        ]),
        el("span", {
          className: "cmd-sub",
          text: user.region_id === null ? t("brandJurisdictionAll") : t("brandJurisdictionRegion"),
        }),
      ]),
    ]);

    const personaSel = el("select", { className: "hdr-select", ariaLabel: "Active Persona" });
    for (const p of this.adapter.listPersonas()) {
      const opt = el("option", { value: p.username, text: `${p.full_name}` });
      if (p.username === user.username) opt.selected = true;
      personaSel.appendChild(opt);
    }
    personaSel.addEventListener("change", () => {
      void this.adapter
        .switchPersona(personaSel.value)
        .then(async (nextUser) => {
          const passphrase =
            this.adapter.mode === "live"
              ? "QuanticoDemo2026!"
              : `quantico-censo-demo-vault::${nextUser.username}`;
          await this.unlockVault(passphrase);
          await this.refreshAll();
        })
        .catch((err) => {
          this.renderHeader();
          this.notify(err instanceof Error ? err.message : "Switch failed", "danger");
        });
    });

    const langBtn = el("button", {
      className: "hdr-btn hdr-slate",
      type: "button",
      text: lang === "es" ? "EN" : "ES",
      onClick: () => setLang(lang === "es" ? "en" : "es"),
    });

    const toolsMenu = el("div", { className: `tools-menu ${this.toolsMenuOpen ? "" : "hidden"}` }, [
      el("button", {
        type: "button",
        text: `📍 ${t("toolManualCensus")}`,
        onClick: () => {
          this.toolsMenuOpen = false;
          this.renderHeader();
          this.commandView.enableManualCensusMode();
        },
      }),
      el("button", {
        type: "button",
        text: `📊 ${t("toolExportCsv")}`,
        onClick: () => {
          this.toolsMenuOpen = false;
          this.renderHeader();
          this.commandView.exportVisibleCsv();
        },
      }),
      el("button", {
        type: "button",
        text: `👥 ${t("toolUsers")}`,
        onClick: () => {
          this.toolsMenuOpen = false;
          this.renderHeader();
          this.commandView.openModal("users");
        },
      }),
      el("button", {
        type: "button",
        text: this.adapter.mode === "demo" ? `🔌 ${t("toolConnectApi")}` : `🧪 ${t("toolDemoMode")}`,
        onClick: () => {
          this.toolsMenuOpen = false;
          if (this.adapter.mode === "live") {
            void this.liveAdapter.disconnect().then(async () => {
              this.adapter = this.demoAdapter;
              await this.unlockVault(`quantico-censo-demo-vault::${this.adapter.getCurrentUser().username}`);
              this.bindRealtime();
              await this.refreshAll();
            });
          } else {
            this.showLiveModal = true;
            this.renderHeader();
            this.renderLiveModal();
          }
        },
      }),
    ]);

    const toolsWrap = el("div", { className: "tools-wrap" }, [
      el("button", {
        className: "hdr-btn hdr-slate",
        type: "button",
        text: `⚙️ ${t("btnTools")} ▾`,
        onClick: () => {
          this.toolsMenuOpen = !this.toolsMenuOpen;
          this.renderHeader();
        },
      }),
      toolsMenu,
    ]);

    const controls = el("div", { className: "cmd-controls" }, [
      el("button", {
        className: "hdr-btn hdr-emerald",
        type: "button",
        text: `📱 ${t("btnMobileApp")}`,
        onClick: () => this.switchSurface("pwa"),
      }),
      el(
        "button",
        {
          className: "hdr-btn hdr-orange",
          type: "button",
          onClick: () => this.commandView.openModal("triage"),
        },
        [
          `🚨 ${t("btnTriage")}`,
          badges.triage > 0 ? el("span", { className: "hdr-badge", text: String(badges.triage) }) : null,
        ],
      ),
      el(
        "button",
        {
          className: "hdr-btn hdr-sky",
          type: "button",
          onClick: () => this.commandView.openModal("support"),
        },
        [
          `🎧 ${t("btnSupport")}`,
          badges.tickets > 0 ? el("span", { className: "hdr-badge", text: String(badges.tickets) }) : null,
        ],
      ),
      el("button", {
        className: "hdr-btn hdr-blue",
        type: "button",
        text: `🚚 ${t("btnDispatch")}`,
        onClick: () => this.commandView.openModal("dispatch"),
      }),
      el("button", {
        className: "hdr-btn hdr-teal",
        type: "button",
        text: `📊 ${t("btnStats")}`,
        onClick: () => this.commandView.openModal("stats"),
      }),
      toolsWrap,
      personaSel,
      langBtn,
    ]);

    this.headerSlot.appendChild(el("header", { className: "cmd-header" }, [brand, controls]));
  }

  private renderLiveModal(): void {
    clearNode(this.liveModalHost);
    if (!this.showLiveModal) return;

    let username = "admin_hq";
    let password = "QuanticoDemo2026!";
    const close = () => {
      this.showLiveModal = false;
      this.renderLiveModal();
    };

    const backdrop = el("div", {
      className: "modal-backdrop",
      onClick: (ev) => {
        if (ev.target === backdrop) close();
      },
    });
    const dialog = el("div", { className: "modal-dialog modal-sm" }, [
      el("div", { className: "modal-head" }, [
        el("h3", { text: t("liveLoginTitle") }),
        el("button", { className: "icon-close-btn", type: "button", text: "×", onClick: close }),
      ]),
      el("div", { className: "modal-body stack-sm" }, [
        el("label", { className: "field-label" }, [
          el("span", { text: t("lblUsername") }),
          el("input", {
            className: "field-input",
            value: username,
            onInput: (ev) => {
              username = (ev.target as HTMLInputElement).value;
            },
          }),
        ]),
        el("label", { className: "field-label" }, [
          el("span", { text: t("lblPassword") }),
          el("input", {
            className: "field-input",
            type: "password",
            value: password,
            onInput: (ev) => {
              password = (ev.target as HTMLInputElement).value;
            },
          }),
        ]),
        el("div", { className: "action-row" }, [
          el("button", {
            className: "btn-solid-primary",
            type: "button",
            text: t("btnSignIn"),
            onClick: () => {
              void this.liveAdapter
                .loginLive(username, password)
                .then(async () => {
                  this.adapter = this.liveAdapter;
                  await this.unlockVault(password);
                  this.showLiveModal = false;
                  this.bindRealtime();
                  await this.refreshAll();
                  this.notify(t("toastLiveConnected"), "ok");
                })
                .catch((err) => this.notify(err instanceof Error ? err.message : "Error de acceso", "danger"));
            },
          }),
          el("button", {
            className: "btn-outline-primary",
            type: "button",
            text: t("cancel"),
            onClick: close,
          }),
        ]),
      ]),
    ]);
    backdrop.appendChild(dialog);
    this.liveModalHost.appendChild(backdrop);
  }
}

const mount = document.getElementById("app");
if (mount) {
  const shell = new QuanticoCensoShell(mount);
  void shell.boot();
}
