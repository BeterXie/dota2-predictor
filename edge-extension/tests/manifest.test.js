import test from "node:test";
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";

const manifest = JSON.parse(await readFile(new URL("../manifest.json", import.meta.url), "utf8"));

test("manifest has the fixed minimal permission surface", () => {
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.permissions.sort(), ["alarms", "storage"]);
  assert.deepEqual(manifest.host_permissions.sort(), [
    "http://127.0.0.1/*",
    "https://cfinfo.365raylinks.com/*",
    "https://www.ray086.com/*",
  ]);
  const forbidden = ["cookies", "tabs", "history", "webRequest", "webRequestBlocking", "<all_urls>"];
  for (const permission of forbidden) assert.ok(!manifest.permissions.includes(permission));
  assert.equal(manifest.background.type, "module");
});
