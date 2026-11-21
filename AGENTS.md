# AGENTS.md — atomberg-local

Executors here follow /home/agent/codeman-cases/Sam/agents/executor.md and their brief; Sam orchestrates.

## What this is

Small Python scripts + guide for Atomberg fans over local LAN (UDP) and cloud API.
Identify fans by MAC→IP via beacons, read state broadcasts, send speed/power/LED commands.
See `README.md` (source of truth; based on Atomberg OpenAPI spec + overview docs).

## Layout

- `atomberg_*.py` — root scripts: beacons, state, discovery, send-command.
- `Documentation/` — `architecture/codebase-graph.json` generated graph.
- `generator/` — `project_graph.py` + extractors/manifest/diagnostics (graph tooling).
- `.github/` — `workflows/graph-freshness.yml`, `tests/test-graph-freshness.sh` (read-only gate).
- `.cache-docs/` — cached `openapi.yaml` reference.
- Root config: `graph.config.json`, `manifest.schema.json`, `graph-template-VERSION`.

## Build / package / test

None documented. No `package.json`, `Makefile`, `pyproject`, or install step in `README.md`.
Runtime use only (per `README.md`): `python .\atomberg_listen_beacons.py --seconds 6`,
`python .\atomberg_send_command.py --ip 192.168.0.13 --speed 3`,
`python .\atomberg_listen_state.py --seconds 8`.
Graph gate (not a build): `bash .github/tests/test-graph-freshness.sh`.

## Graph

- `/home/agent/codeman-cases/Sam/scripts/sam-graph.sh find atomberg --project atomberg-local`
- `/home/agent/codeman-cases/Sam/scripts/sam-graph.sh impact <paths> --project atomberg-local`
- Node: `project:atomberg-local`.

## Safety

Secrets names only; never print values, env, keys, or tokens.
Never push to the default branch (`main`); work on `task/agents-md`, open a PR.
