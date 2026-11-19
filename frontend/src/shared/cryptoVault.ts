/**
 * Off-main-thread Web Crypto vault client with worker crash recovery & safe main-thread fallback.
 */
import CryptoWorkerCtor from "./cryptoWorker?worker&inline";

interface PendingRpc {
  resolve: (data: Record<string, string>) => void;
  reject: (err: Error) => void;
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

function base64ToBytes(b64: string): Uint8Array<ArrayBuffer> {
  const binary = atob(b64);
  const buffer = new ArrayBuffer(binary.length);
  const out = new Uint8Array(buffer);
  for (let i = 0; i < binary.length; i += 1) {
    out[i] = binary.charCodeAt(i);
  }
  return out;
}

export function generateRandomSaltB64(): string {
  return bytesToBase64(crypto.getRandomValues(new Uint8Array(16)));
}

export class CryptoVaultClient {
  private worker: Worker | null = null;
  private seq = 1;
  private pending = new Map<number, PendingRpc>();
  private fallbackKey: CryptoKey | null = null;
  private useFallback = false;
  private lastPassphrase = "";
  private lastSaltB64 = "";

  private ensureWorker(): Worker | null {
    if (this.useFallback) return null;
    if (this.worker) return this.worker;
    try {
      const w = new CryptoWorkerCtor();
      w.onmessage = (ev: MessageEvent<{ id: number; ok: boolean; data?: Record<string, string>; error?: string }>) => {
        const msg = ev.data;
        const job = this.pending.get(msg.id);
        if (!job) return;
        this.pending.delete(msg.id);
        if (msg.ok && msg.data) {
          job.resolve(msg.data);
        } else {
          job.reject(new Error(msg.error || "Worker rejected operation"));
        }
      };
      w.onerror = (err: ErrorEvent) => {
        for (const job of this.pending.values()) {
          job.reject(new Error(err.message || "Crypto worker crashed"));
        }
        this.pending.clear();
        w.terminate();
        this.worker = null;
        this.useFallback = true;
      };
      this.worker = w;
      return w;
    } catch (err) {
      console.warn("Web Worker crypto unavailable, using Web Crypto fallback", err);
      this.useFallback = true;
      return null;
    }
  }

  private async ensureFallbackKey(): Promise<CryptoKey> {
    if (this.fallbackKey) return this.fallbackKey;
    if (!this.lastPassphrase || !this.lastSaltB64) {
      throw new Error("Vault key not unlocked");
    }
    const material = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(this.lastPassphrase),
      "PBKDF2",
      false,
      ["deriveKey"],
    );
    this.fallbackKey = await crypto.subtle.deriveKey(
      { name: "PBKDF2", salt: base64ToBytes(this.lastSaltB64), iterations: 100_000, hash: "SHA-256" },
      material,
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt", "decrypt"],
    );
    return this.fallbackKey;
  }

  private rpc(payload: Record<string, unknown>): Promise<Record<string, string>> {
    const w = this.ensureWorker();
    if (!w) return Promise.reject(new Error("Worker unavailable"));
    const id = this.seq++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      w.postMessage({ id, ...payload });
    });
  }

  async unlock(passphrase: string, saltB64: string): Promise<void> {
    this.lastPassphrase = passphrase;
    this.lastSaltB64 = saltB64;
    this.fallbackKey = null;

    if (!this.useFallback) {
      try {
        await this.rpc({ op: "deriveKey", passphrase, saltB64 });
        return;
      } catch (err) {
        console.warn("Worker key derivation fell back to main thread Web Crypto", err);
        this.useFallback = true;
      }
    }
    await this.ensureFallbackKey();
  }

  async encryptJson(obj: unknown): Promise<{ ivB64: string; ciphertextB64: string }> {
    const plaintext = JSON.stringify(obj);
    if (!this.useFallback) {
      try {
        const res = await this.rpc({ op: "encrypt", plaintext });
        return { ivB64: res.ivB64, ciphertextB64: res.ciphertextB64 };
      } catch (err) {
        console.warn("Worker encrypt failed, switching to fallback", err);
        this.useFallback = true;
      }
    }
    const key = await this.ensureFallbackKey();
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const cipherBuf = await crypto.subtle.encrypt(
      { name: "AES-GCM", iv },
      key,
      new TextEncoder().encode(plaintext),
    );
    return {
      ivB64: bytesToBase64(iv),
      ciphertextB64: bytesToBase64(new Uint8Array(cipherBuf)),
    };
  }

  async decryptJson<T>(ivB64: string, ciphertextB64: string): Promise<T> {
    if (!this.useFallback) {
      try {
        const res = await this.rpc({ op: "decrypt", ivB64, ciphertextB64 });
        return JSON.parse(res.plaintext) as T;
      } catch (err) {
        // Distinguish Web Worker crash (useFallback flipped to true) from genuine AES-GCM auth/key error
        if (!this.useFallback) throw err;
      }
    }
    const key = await this.ensureFallbackKey();
    const plainBuf = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: base64ToBytes(ivB64) },
      key,
      base64ToBytes(ciphertextB64),
    );
    return JSON.parse(new TextDecoder().decode(plainBuf)) as T;
  }

  dispose(): void {
    for (const job of this.pending.values()) {
      job.reject(new Error("Vault worker disposed"));
    }
    this.pending.clear();
    if (this.worker) {
      this.worker.terminate();
      this.worker = null;
    }
    this.fallbackKey = null;
    this.lastPassphrase = "";
    this.lastSaltB64 = "";
  }
}

export const cryptoVault = new CryptoVaultClient();
