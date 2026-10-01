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
- A checkout of the `canonic` repository (the compose file builds the daemon from
  `Dockerfile` in this directory)
- Network access from inside the `canonic` container to clone
  [`canonic-packs`](https://github.com/mischuh/canonic-packs) from GitHub at startup —
  no local checkout needed, see "Where the pack content comes from" below

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

## Where the pack content comes from

`entrypoint.sh` runs `canonic pack add posthog` with no `--repo`, so it falls back to the
command's own built-in default: the `canonic-packs` GitHub repo
(`canonic/cli/commands/pack.py::_DEFAULT_REPO`), shallow-cloned fresh into
`.canonic/packs-cache/` on every container start. The repo's own root `Dockerfile` has no
`git` binary (adding one would grow every image built from it, not just this demo), so
this directory has its own `Dockerfile` — an exact copy of the root one plus `git` —
built instead. Keep the two in sync if the root `Dockerfile` changes.

## Point this at a real PostHog instance

Nothing about the pack mechanism changes: swap `canonic.docker.yaml`'s `posthog_db`
connection for your real warehouse's batch-export database, and `pack-params.json`'s
`signup_event`/`activation_event` for your actual event names (or drop
`--params-file`/`--yes` from `entrypoint.sh` and run `canonic pack add posthog`
interactively — `choose_from` then offers a live top-50 event picker against your data).
