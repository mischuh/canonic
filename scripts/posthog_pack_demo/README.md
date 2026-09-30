# PostHog context-pack demo

Brings up a real Postgres database seeded with data shaped exactly like [PostHog's
documented Postgres batch-export
format](https://posthog.com/docs/cdp/batch-exports/postgres), plus a dockerized Canonic
MCP daemon that installs the [`canonic-packs`](https://github.com/mischuh/canonic-packs)
**posthog** context pack against it on startup and then serves the result over MCP — the
same "run one command, get a
queryable project" experience [`scripts/local_idp/`](../local_idp/) gives for the
OAuth/RBAC story, but for the context-pack install mechanism itself.

The full guide, including a walkthrough of the installed metrics and the
internal-traffic guardrail, lives in the docs: [PostHog context
pack](../../docs/guides/posthog-context-pack.mdx)
([online](https://docs.getcanonic.app/guides/posthog-context-pack)). This README is the
short runnable reference.

## Prerequisites

- Docker with Compose
- A checkout of the `canonic` repository (the compose file builds the daemon from the
  repo's own `Dockerfile`)
- A checkout of [`canonic-packs`](https://github.com/mischuh/canonic-packs) on the
  `feat/posthog-postgres-pack` branch, as a sibling directory of this repo
  (`../canonic-packs` relative to `canonic/`) — or set `CANONIC_PACKS_DIR` to point
  elsewhere. See "Why a local checkout, not `--repo <url>`" below.

## Start

From the repository root:

```bash
docker compose -f scripts/posthog_pack_demo/docker-compose.yml up --build
```

Wait for `installing the posthog context pack...` followed by a line reporting the
installed files, then Uvicorn's `Uvicorn running on http://0.0.0.0:7474` line.

| Service | URL | Notes |
| --- | --- | --- |
| Postgres | `localhost:5432` (from the host, not published by default) | seeded with `posthog_exports.events`/`persons`, see `postgres/init/` |
| Canonic MCP endpoint | `http://localhost:7474` | serves the freshly pack-installed project |

## What gets installed

Every container start runs `canonic pack add posthog --variant postgres --connection
posthog_db --params-file pack-params.json --yes` (see `entrypoint.sh`), which writes:

- `semantics/posthog_db/ph_events.yaml`, `ph_persons.yaml`
- `contracts/metrics/{active_users,new_users,activated_users,activation_rate}.yaml`
- `contracts/guardrails/posthog-exclude-internal-traffic.yaml`
- `knowledge/global/{activation-definition,internal-traffic-caveat}.md`

Nothing here is checked into this demo directory — `canonic.docker.yaml` ships only the
`posthog_db` connection, and everything else is generated fresh on each `up` (the
project directory isn't a persisted volume). See the pack's own README in
`canonic-packs/packs/posthog/README.md` for what each file does.

## Query it

```bash
curl -s http://localhost:7474/mcp \
  -H "Authorization: Bearer posthog-demo-local-dev" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Or point an MCP client (e.g. the [MCP
Inspector](https://github.com/modelcontextprotocol/inspector)) at
`http://localhost:7474` with that same bearer token and query `active_users`,
`new_users`, or `activated_users`. `posthog-demo-local-dev` is `canonic.docker.yaml`'s
single static demo token (`mcp.auth.tokens`) — `--transport http` refuses to start with
no auth mechanism configured at all.

> `activation_rate` (a ratio of two `distinct_count` metrics) currently fails with
> `unsupported_measure: nested composite metrics are not yet supported` — a pre-existing
> canonic query-engine limitation, not a pack or demo defect. Query `activated_users` and
> `new_users` separately and divide for the same number in the meantime. See the full
> guide for details.

## Stop

```bash
docker compose -f scripts/posthog_pack_demo/docker-compose.yml down
```

Add `-v` to also drop the seeded Postgres volume (a plain `down`/`up` reuses it and skips
re-seeding, since Postgres only runs `postgres/init/*.sql` against a fresh volume).

## Why a local checkout, not `--repo <url>`

The posthog pack's content lives on `canonic-packs`' `feat/posthog-postgres-pack` branch,
not yet merged to `main`, and the base `Dockerfile` has no `git` binary installed, so
`canonic pack add --repo https://github.com/mischuh/canonic-packs.git` can't clone from
inside the container today. `docker-compose.yml` instead bind-mounts a local
`canonic-packs` checkout read-only at `/packs/canonic-packs` and passes that as `--repo`.
Once the branch merges to `main`, this mount can be dropped in favor of the `--repo`
default `canonic pack add` already falls back to
(`canonic/cli/commands/pack.py::_DEFAULT_REPO`) — a one-line change in `entrypoint.sh`.

## Point this at a real PostHog instance

Nothing about the pack mechanism changes: swap `canonic.docker.yaml`'s `posthog_db`
connection for your real warehouse's batch-export database, and `pack-params.json`'s
`signup_event`/`activation_event` for your actual event names (or drop
`--params-file`/`--yes` from `entrypoint.sh` and run `canonic pack add posthog`
interactively — `choose_from` then offers a live top-50 event picker against your data).
