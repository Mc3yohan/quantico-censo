/**
 * Singleton IndexedDB Encrypted Vault (`quantico_censo_vault_v2`), partitioned strictly by `user_scope`.
 */
import type { VaultEncryptedEntry } from "./types";
import { generateRandomSaltB64 } from "./cryptoVault";

const DB_NAME = "quantico_censo_vault_v2";
const DB_VERSION = 1;
const STORE_AUDITS = "encrypted_audits";
const STORE_META = "vault_meta";

let dbPromise: Promise<IDBDatabase> | null = null;
let memFallbackEntries: VaultEncryptedEntry[] = [];
const memFallbackSalts = new Map<string, string>();

function openDb(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise<IDBDatabase>((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      dbPromise = null;
      reject(new Error("IndexedDB unavailable"));
      return;
    }
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE_AUDITS)) {
        const store = db.createObjectStore(STORE_AUDITS, { keyPath: "sync_uuid" });
        store.createIndex("by_scope", "user_scope", { unique: false });
      }
      if (!db.objectStoreNames.contains(STORE_META)) {
        db.createObjectStore(STORE_META, { keyPath: "key" });
      }
    };
    req.onsuccess = () => {
      const db = req.result;
      db.onversionchange = () => {
        db.close();
        dbPromise = null;
      };
      resolve(db);
    };
    req.onerror = () => {
      dbPromise = null;
      reject(req.error || new Error("Failed to open IndexedDB"));
    };
  });
  return dbPromise;
}

export async function getOrCreateVaultSalt(userScope: string): Promise<string> {
  const key = `salt::${userScope}`;
  try {
    const db = await openDb();
    const existing = await new Promise<string | null>((resolve, reject) => {
      const tx = db.transaction(STORE_META, "readonly");
      const req = tx.objectStore(STORE_META).get(key);
      req.onsuccess = () => {
        const rec = req.result as { key: string; value: string } | undefined;
        resolve(rec ? rec.value : null);
      };
      req.onerror = () => reject(req.error || new Error("Read salt failed"));
    });
    if (existing) return existing;

    const freshSalt = generateRandomSaltB64();
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE_META, "readwrite");
      tx.objectStore(STORE_META).put({ key, value: freshSalt });
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error("Persist salt failed"));
    });
    return freshSalt;
  } catch (err) {
    console.warn("IndexedDB salt fallback active", err);
    const existing = memFallbackSalts.get(key);
    if (existing) return existing;
    const fresh = generateRandomSaltB64();
    memFallbackSalts.set(key, fresh);
    return fresh;
  }
}

export async function putVaultEntry(entry: VaultEncryptedEntry): Promise<void> {
  try {
    const db = await openDb();
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE_AUDITS, "readwrite");
      tx.objectStore(STORE_AUDITS).put(entry);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error("Vault write failed"));
    });
  } catch (err) {
    console.warn("IndexedDB write fell back to volatile memory", err);
    memFallbackEntries = [entry, ...memFallbackEntries.filter((e) => e.sync_uuid !== entry.sync_uuid)];
  }
}

export async function listVaultEntries(userScope: string): Promise<VaultEncryptedEntry[]> {
  try {
    const db = await openDb();
    return await new Promise<VaultEncryptedEntry[]>((resolve, reject) => {
      const tx = db.transaction(STORE_AUDITS, "readonly");
      const req = tx.objectStore(STORE_AUDITS).getAll();
      req.onsuccess = () => {
        const rows = ((req.result as VaultEncryptedEntry[]) || []).filter(
          (r) => r.user_scope === userScope,
        );
        rows.sort((a, b) => b.captured_at.localeCompare(a.captured_at));
        resolve(rows);
      };
      req.onerror = () => reject(req.error || new Error("Vault list failed"));
    });
  } catch (err) {
    console.warn("IndexedDB list fell back to volatile memory", err);
    return memFallbackEntries.filter((r) => r.user_scope === userScope);
  }
}

export async function deleteVaultEntry(syncUuid: string): Promise<void> {
  try {
    const db = await openDb();
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE_AUDITS, "readwrite");
      tx.objectStore(STORE_AUDITS).delete(syncUuid);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error("Vault delete failed"));
    });
  } catch (err) {
    console.warn("IndexedDB delete fell back to volatile memory", err);
    memFallbackEntries = memFallbackEntries.filter((e) => e.sync_uuid !== syncUuid);
  }
}
