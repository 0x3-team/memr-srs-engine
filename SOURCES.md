# Sources

- Engine source id: `d8b5ba045d3a` (published tag `engine-d8b5ba045d3a`)
- Source repository: https://github.com/0x3-team/memr-srs-engine
- Memr commit: `657382c9fb6c072de01894f702c36c759424bc95`
- Build: `docker build -f srs-engine/Dockerfile.cloud .` from the bundle root
- The Node binary comes from the public `node:24-bookworm-slim` image.

## Pinned Python packages

- `anki==26.9.3` — https://github.com/ankitects/anki/tree/26.09.3
- `certifi==2026.7.22` — https://pypi.org/project/certifi/2026.7.22/
- `charset-normalizer==3.5.2` — https://pypi.org/project/charset-normalizer/3.5.2/
- `decorator==5.3.1` — https://pypi.org/project/decorator/5.3.1/
- `distro==1.9.0` (env marker: `sys_platform != 'darwin' and sys_platform != 'win32'`) — https://pypi.org/project/distro/1.9.0/
- `idna==3.20` — https://pypi.org/project/idna/3.20/
- `markdown==3.10.3` (env marker: `python_full_version < '3.11'`) — https://pypi.org/project/markdown/3.10.3/
- `markdown==3.11` (env marker: `python_full_version >= '3.11'`) — https://pypi.org/project/markdown/3.11/
- `orjson==3.13.0` — https://pypi.org/project/orjson/3.13.0/
- `protobuf==7.36.2` — https://pypi.org/project/protobuf/7.36.2/
- `pysocks==1.7.1` — https://pypi.org/project/pysocks/1.7.1/
- `requests==2.34.2` — https://pypi.org/project/requests/2.34.2/
- `typing-extensions==4.16.0` — https://pypi.org/project/typing-extensions/4.16.0/
- `urllib3==2.8.0` — https://pypi.org/project/urllib3/2.8.0/
- `zstandard==0.25.0` — https://pypi.org/project/zstandard/0.25.0/
