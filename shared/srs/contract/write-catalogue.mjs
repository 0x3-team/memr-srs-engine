// The one generator for shared/srs/contract/catalogue/*.json — the operation
// catalogue the published flashcard engine loads. This module is part of the
// engine's AGPL source (it ships in the published bundle and in the engine
// image), so the catalogue can be regenerated from source with
// `node shared/srs/contract/write-catalogue.mjs` run from the bundle root.
// `npm run srs:contract:write` and `npm run srs:contract:check` go through the
// same catalogueFiles() map, so there is exactly one catalogue generator.

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { areas, CONTRACT_VERSION } from "./index.mjs";

/**
 * `Map<study-platform-relative path, file contents>` for every catalogue
 * JSON: one file per contract area plus `index.json` listing them.
 */
export function catalogueFiles() {
  const files = new Map();
  for (const [area, entries] of Object.entries(areas)) {
    files.set(`shared/srs/contract/catalogue/${area}.json`, `${JSON.stringify({ version: CONTRACT_VERSION, area, methods: entries }, null, 2)}\n`);
  }
  files.set("shared/srs/contract/catalogue/index.json", `${JSON.stringify({ version: CONTRACT_VERSION, areas: Object.keys(areas).map((area) => `${area}.json`) }, null, 2)}\n`);
  return files;
}

/** Write every catalogue file under `root` (defaults to the study-platform root). */
export async function writeCatalogue(root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..")) {
  for (const [rel, text] of catalogueFiles()) {
    const target = join(root, rel);
    await mkdir(dirname(target), { recursive: true });
    await writeFile(target, text);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await writeCatalogue();
}
