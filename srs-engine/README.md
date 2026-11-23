# Memr flashcard engine

The Memr flashcard engine is a JSON-lines/HTTP adapter that exposes Memr's
operation catalogue over Anki 26.09.3. It runs the official `anki==26.9.3`
package unmodified inside an isolated interpreter and answers one operation
per request line.

This repository (or source bundle) is the exact source of the engine that
the hosted Memr application runs. Memr itself is a separate proprietary
product; this engine is published on its own because Anki is licensed
AGPL-3.0-or-later.

## Licence

Copyright (C) 2026 Human Insight LLC.

The engine is free software under the GNU Affero General Public License,
version 3 or later (`AGPL-3.0-or-later`). The complete licence text is in
`LICENSE`; the copyright and attribution summary is in `NOTICE`.

The engine embeds Anki 26.09.3 unmodified (AGPL-3.0-or-later, with
contributed portions under BSD-3-Clause) and its pinned Python dependency
closure. `licenses/` preserves every third-party notice that ships with the
container image:

| Path | Covers |
| --- | --- |
| `licenses/anki-26.9.3/` | Anki `LICENSE` and `CONTRIBUTORS` |
| `licenses/fsrs-6.6.2/` | FSRS, compiled into Anki's wheel (BSD-3-Clause) |
| `licenses/python-zstandard-0.25.0.LICENSE.txt` | python-zstandard (BSD) |
| `licenses/zstd-1.5.7.LICENSE.txt` | bundled zstd (BSD terms) |

Redistribution must retain the python-zstandard and bundled zstd copyright,
conditions and disclaimers: ship those two files with installer, source and
binary distributions that include the dependency. Do not use upstream names
as product endorsements. The bundled zstd source offers BSD or GPLv2; the
preserved notice documents the BSD terms.

## Bundle layout

- `srs-engine/Dockerfile.cloud` — the container build (see below).
- `srs-engine/requirements.txt` — the complete pinned dependency closure;
  every permitted artifact carries a `--hash=sha256` line. Installers must
  pass `--require-hashes` and `--only-binary :all:`.
- `srs-engine/launch.py` — runs the JSON-lines engine on stdio.
- `srs-engine/http_shim.py` — single-collection HTTP shim (what the
  container entrypoint runs).
- `srs-engine/memr_srs_engine/` — the engine package: the JSON-lines
  protocol, collection owner, handler registry, one module per operation
  area, and the standalone workers Memr's host runs by path
  (`backup_validation.py` for backup checks, `sync_server_parent.py` to
  supervise the bundled Anki sync server).
- `srs-engine/licenses/` — third-party licences, copied into the image.
- `srs-engine/tests/` — the engine's `unittest` suite.
- `shared/srs/contract/` — the operation contract: the JSON catalogue
  (`catalogue/*.json`, one file per area: `decks`, `cards`, `notes`,
  `study`, `sync`, …) describing every method name, parameter shape and
  result contract the adapter implements, plus the modules that generate
  it (`write-catalogue.mjs`, the contract sources it imports and their
  `.d.mts` typings).
- `LICENSE`, `NOTICE`, `README.md` — this licence set.
- `SOURCES.md` — generated provenance: the engine source id, the Memr
  commit, the upstream source location of every pinned package, and the
  build command.
- `dependency-sources/` — present only when the bundle was generated with
  `--with-dependency-sources`: the Anki tag tarball and the sdist of every
  other pinned package.

## Regenerating the catalogue

The catalogue JSON under `shared/srs/contract/catalogue/` is generated — it is
not hand-edited. From the bundle root:

```sh
node shared/srs/contract/write-catalogue.mjs
```

reproduces every `catalogue/*.json` byte-for-byte from the contract modules
(`index.mjs` and the per-area sources it imports). In the Memr tree,
`npm run srs:contract:write` regenerates through the same single generator and
`npm run srs:contract:check` fails if committed output drifts from it.

## Build and run

From the bundle root:

```sh
docker build -f srs-engine/Dockerfile.cloud -t memr-srs-engine .
```

The image is based on `python:3.12-slim-bookworm`, adds the Node binary from
the public `node:24-bookworm-slim` image (required by the sync bridge and by
Node's built-in zstd streams), installs the pinned requirements with
`--require-hashes --only-binary :all:`, verifies `anki.collection` and the
zstandard C backend inside the image, and runs as a non-root user with no
pip and a read-only layout. Its entrypoint starts the HTTP shim on port
8080:

```sh
docker run --rm -p 8080:8080 memr-srs-engine
```

`POST /rpc` forwards one JSON-RPC request to the engine in FIFO order and
`GET /healthz` is the readiness probe; `http_shim.py` documents the
collection transfer endpoints. With `MEMR_SRS_REQUIRE_RESTORE=1` (set by the
image) the shim exposes `/restore`, `/snapshot`, `/media-index` and
`/media/<name>` and refuses `/rpc` until a restore succeeds.

Outside the container the same engine runs on stdio — one JSON request per
line, one JSON result per line:

```sh
python -I srs-engine/launch.py --collection /path/to/collection-dir
python -I srs-engine/launch.py --describe   # prints the registered operations
```

`launch.py` puts only its own directory on `sys.path` and then runs
`memr_srs_engine`, so it works under `-I` isolation. Run it with the pinned
requirements installed (CPython 3.10–3.14; see the matrix below).

The test suite is stdlib `unittest`; from the bundle root:

```sh
python -I -B -m unittest discover -s srs-engine/tests -t srs-engine
```

## Exact versions and provenance

- Engine interpreter dependencies: `anki==26.9.3`, `zstandard==0.25.0` and
  the complete pinned closure in `srs-engine/requirements.txt`.
- Anki upstream source: <https://github.com/ankitects/anki/tree/26.09.3>.
- Other pinned packages: `https://pypi.org/project/<name>/<version>/`.
- `SOURCES.md` in a generated bundle records the engine source id, the Memr
  commit it was cut from, and each package's upstream location.

`requirements.txt` pins 29 supported platform/Python combinations:

| Platform | Architecture | CPython |
| --- | --- | --- |
| Linux, glibc 2.35+ | x86_64, aarch64 | 3.10–3.14 |
| macOS 12+ | x86_64, arm64 | 3.10–3.14 |
| Windows | x64 | 3.10–3.14 |
| Windows | arm64 | 3.11–3.14 |

Anki's six `cp310-abi3` wheels cover those targets, but the pinned orjson
3.13.0 release supplies Windows arm64 wheels only from CPython 3.11; an
installer must request `>=3.11,<3.15` there (`>=3.10,<3.15` elsewhere) and
refuse 3.10 on Windows arm64. Markdown is pinned at 3.10.3 below Python 3.11
and at 3.11 above it. Protobuf is pinned within Anki's `>=6,<8` constraint.
Platform hash coverage is distinct from running integration tests on each
operating system.

The zstandard pin adds CPython C-extension wheels for all 29 existing
cells. The manifest includes all 98 published wheel hashes plus its source
archive hash; `--only-binary` still refuses source builds.

## Source bundles, ids and tags

Each published engine revision is tagged `engine-<id>`, where `<id>` is the
first 12 hex characters of the SHA-256 over the exact file set above (each
file fed to the hash as `path + "\0" + bytes + "\0"` in sorted order).
Bundle directories are named `memr-srs-engine-<id>/` and carry a generated
`SOURCES.md` with the id, the Memr commit and per-package upstream sources.
The hosted Memr application links this exact id as "Flashcard engine source
code". Reproducing the bundle from the same tree gives the same id.

## Disclaimer

Anki is a trademark of Anki Software, LLC. Memr is not affiliated with or
endorsed by Anki Software, LLC.
