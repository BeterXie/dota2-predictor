import test from "node:test";
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";

import {companionConstants} from "../src/companion-client.js";

await import("../src/constants.js");

const manifest = JSON.parse(await readFile(new URL("../manifest.json", import.meta.url), "utf8"));
const packageMetadata = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url), "utf8"),
);
const backendSource = await readFile(
  new URL("../../live_betting/browser_companion.py", import.meta.url),
  "utf8",
);
const contractSource = await readFile(
  new URL("../../live_betting/browser_contract.py", import.meta.url),
  "utf8",
);

test("manifest has the fixed minimal permission surface", () => {
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.permissions.sort(), ["alarms", "storage"]);
  assert.deepEqual(manifest.host_permissions.sort(), [
    "http://127.0.0.1/*",
    "https://cfinfo.365raylinks.com/*",
    "https://iminfo.esportsworldlink.com/*",
    "https://ray086.com/*",
    "https://www.ray086.com/*",
  ]);
  const forbidden = ["cookies", "tabs", "history", "webRequest", "webRequestBlocking", "<all_urls>"];
  for (const permission of forbidden) assert.ok(!manifest.permissions.includes(permission));
  assert.equal(manifest.background.type, "module");
  for (const script of manifest.content_scripts) {
    assert.deepEqual(script.matches.sort(), [
      "https://ray086.com/*",
      "https://www.ray086.com/*",
    ]);
    assert.equal(script.all_frames, false);
  }
});

test("extension and companion protocol versions cannot drift independently", () => {
  const backendExtension = backendSource.match(/SUPPORTED_EXTENSION_VERSION = "([^"]+)"/)?.[1];
  const backendProtocol = Number(backendSource.match(/PROTOCOL_VERSION = (\d+)/)?.[1]);
  const backendSchema = Number(contractSource.match(/SCHEMA_VERSION = (\d+)/)?.[1]);
  const backendGame = Number(contractSource.match(/DOTA2_GAME_ID = (\d+)/)?.[1]);

  assert.equal(packageMetadata.version, manifest.version);
  assert.equal(globalThis.RaybetMonitor.EXTENSION_VERSION, manifest.version);
  assert.equal(companionConstants.EXTENSION_VERSION, manifest.version);
  assert.equal(backendExtension, manifest.version);
  assert.equal(companionConstants.PROTOCOL_VERSION, backendProtocol);
  assert.equal(globalThis.RaybetMonitor.SCHEMA_VERSION, backendSchema);
  assert.equal(globalThis.RaybetMonitor.DOTA2_GAME_ID, backendGame);
});
