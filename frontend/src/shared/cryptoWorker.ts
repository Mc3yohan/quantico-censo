/**
 * Dedicated Web Worker: PBKDF2-SHA256 (100k rounds) + AES-GCM-256 encryption
 * with random 12-byte IVs and non-extractable CryptoKey.
 */

type WorkerRequest =
  | { id: number; op: "deriveKey"; passphrase: string; saltB64: string }
  | { id: number; op: "encrypt"; plaintext: string }
  | { id: number; op: "decrypt"; ivB64: string; ciphertextB64: string };

type WorkerResponse =
  | { id: number; ok: true; data: Record<string, string> }
  | { id: number; ok: false; error: string };

let derivedKey: CryptoKey | null = null;

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

self.onmessage = async (event: MessageEvent<WorkerRequest>): Promise<void> => {
  const req = event.data;
  try {
    if (req.op === "deriveKey") {
      const encoder = new TextEncoder();
      const keyMaterial = await crypto.subtle.importKey(
        "raw",
        encoder.encode(req.passphrase),
        "PBKDF2",
        false,
        ["deriveKey"],
      );
      const salt = base64ToBytes(req.saltB64);
      derivedKey = await crypto.subtle.deriveKey(
        {
          name: "PBKDF2",
          salt,
          iterations: 100_000,
          hash: "SHA-256",
        },
        keyMaterial,
        { name: "AES-GCM", length: 256 },
        false, // Non-extractable key stays locked inside Web Crypto
        ["encrypt", "decrypt"],
      );
      const res: WorkerResponse = { id: req.id, ok: true, data: { status: "ready" } };
      self.postMessage(res);
      return;
    }

    if (req.op === "encrypt") {
      if (!derivedKey) {
        throw new Error("Vault key not initialized.");
      }
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const encoded = new TextEncoder().encode(req.plaintext);
      const cipherBuf = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, derivedKey, encoded);
      const res: WorkerResponse = {
        id: req.id,
        ok: true,
        data: {
          ivB64: bytesToBase64(iv),
          ciphertextB64: bytesToBase64(new Uint8Array(cipherBuf)),
        },
      };
      self.postMessage(res);
      return;
    }

    if (req.op === "decrypt") {
      if (!derivedKey) {
        throw new Error("Vault key not initialized.");
      }
      const iv = base64ToBytes(req.ivB64);
      const cipher = base64ToBytes(req.ciphertextB64);
      const plainBuf = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, derivedKey, cipher);
      const res: WorkerResponse = {
        id: req.id,
        ok: true,
        data: { plaintext: new TextDecoder().decode(plainBuf) },
      };
      self.postMessage(res);
      return;
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : "Crypto worker failure";
    const res: WorkerResponse = { id: req.id, ok: false, error: message };
    self.postMessage(res);
  }
};
