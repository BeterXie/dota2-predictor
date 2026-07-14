(function installCanonicalJson(root) {
  "use strict";
  const api = root.RaybetMonitor || (root.RaybetMonitor = {});
  const encoder = new TextEncoder();

  function utf8ByteLength(value) {
    return encoder.encode(String(value)).byteLength;
  }

  function canonicalJson(value) {
  const active = new WeakSet();

  function encode(current, inArray = false) {
    if (current === null) return "null";

    const type = typeof current;
    if (type === "string" || type === "boolean") return JSON.stringify(current);
    if (type === "number") return Number.isFinite(current) ? JSON.stringify(current) : "null";
    if (type === "undefined" || type === "function" || type === "symbol") {
      return inArray ? "null" : undefined;
    }
    if (type === "bigint") throw new TypeError("BigInt is not valid JSON");
    if (type !== "object") throw new TypeError("Unsupported JSON value");
    if (active.has(current)) throw new TypeError("Cyclic value is not valid JSON");

    active.add(current);
    let encoded;
    if (Array.isArray(current)) {
      encoded = `[${current.map((item) => encode(item, true) ?? "null").join(",")}]`;
    } else {
      const entries = [];
      for (const key of Object.keys(current).sort()) {
        const item = encode(current[key], false);
        if (item !== undefined) entries.push(`${JSON.stringify(key)}:${item}`);
      }
      encoded = `{${entries.join(",")}}`;
    }
    active.delete(current);
    return encoded;
  }

    return encode(value);
  }

  async function sha256Hex(value, subtle = root.crypto?.subtle) {
    if (!subtle) throw new Error("Web Crypto SHA-256 is unavailable");
    const digest = await subtle.digest("SHA-256", encoder.encode(value));
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  Object.assign(api, { utf8ByteLength, canonicalJson, sha256Hex });
})(globalThis);
