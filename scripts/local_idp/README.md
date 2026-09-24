# Local Identity Provider (Keycloak) for MCP role/permission testing

Brings up a real OIDC identity provider (Keycloak) plus a dockerized Canonic MCP daemon
serving [`examples/marketplace`](../../examples/marketplace), wired together via
`mcp.auth.oauth` in `proxy` mode (`canonic/mcp/auth.py`). Lets you run the actual
Authorization Code + PKCE browser login and see the resulting role/tenant enforcement
(masking, `run_sql` gating, tenancy scoping — SPEC-E12) live, instead of only exercising it
through mocked unit tests.

`examples/marketplace/canonic.yaml` itself is never modified — this setup mounts
[`canonic.docker.yaml`](canonic.docker.yaml) over it inside the container, so the shipped,
golden-tested example stays untouched.

## Start

```sh
docker compose -f scripts/local_idp/docker-compose.yml up --build
```

Wait for:
- Keycloak: log line confirming realm `canonic` was imported.
- `canonic`: Uvicorn's `Uvicorn running on http://0.0.0.0:7474` log line.

Keycloak admin console: http://localhost:8080 (`admin` / `admin`) — useful for inspecting or
editing the imported realm/users. Canonic MCP endpoint: http://localhost:7474.

## Test users (realm `canonic`)

| Username                | Password    | Role              | `merchant_id`  |
|--------------------------|-------------|-------------------|-----------------|
| `byte-gadgets-viewer`    | `viewer`    | `merchant_viewer` | `byte-gadgets`  |
| `byte-gadgets-admin`     | `admin`     | `merchant_admin`  | `byte-gadgets`  |
| `urban-threads-viewer`   | `viewer`    | `merchant_viewer` | `urban-threads` |
| `urban-threads-admin`    | `admin`     | `merchant_admin`  | `urban-threads` |
| `platform-ops`           | `platform`  | `platform_analyst`| *(none — tenancy-exempt)* |

These mirror the five identities already defined as static bearer tokens in
[`examples/marketplace/canonic.yaml`](../../examples/marketplace/canonic.yaml), just issued
through a real IdP login instead of a fixed token. Roles/tenant are read from the token's
`roles` / `merchant_id` claims via Keycloak protocol mappers on the `canonic-mcp` client (see
[`keycloak/canonic-realm.json`](keycloak/canonic-realm.json)) — matching
`examples/marketplace/contracts/policies/roles.yaml`'s `claim: roles` and
`tenancy.yaml`'s `claim: merchant_id`, so no `claim_mapping` is needed.

## Try the OAuth login flow

Connect any MCP client that speaks OAuth 2.1 (e.g. the [MCP
Inspector](https://github.com/modelcontextprotocol/inspector)) to `http://localhost:7474`.
The client performs dynamic client registration against Canonic's own OAuth endpoints;
Canonic redirects the browser to Keycloak for the actual login (`http://localhost:8080`).

1. Log in as `byte-gadgets-viewer` / `viewer`. Query the marketplace metrics grouped by
   `customer_email`. The query is rejected with an `unreachable` error, because
   `merchant_viewer` has `dimensions.deny: [customer_email, customer_phone]` and a denied
   dimension looks exactly like a nonexistent one. The dimension is also missing from
   `describe_metric`, and `run_sql` is refused (`run_sql: false`).
2. Disconnect, reconnect, log in as `byte-gadgets-admin` / `admin` instead. The same query now
   works and `customer_email` comes back **masked** (`ab***`, the `masking: partial` rule of
   `merchant_admin` on `customers.customer_email`). `run_sql` is *accepted by the role* but
   still refused with `TENANT_FORBIDDEN` (`marketplace_db` has `rls_enforced: false`, the
   second `run_sql` gate, see the comment in `roles.yaml`).
3. Log in as `platform-ops` / `platform` and confirm data is visible across *both* merchants
   (`tenancy_exempt: true`, no `merchant_id` claim).

## Quick sanity check without a browser

The static bearer tokens from `canonic.docker.yaml` still work side by side with OAuth
(`CanonicCompositeVerifier` tries them first, falls through to OAuth):

```sh
curl -s http://localhost:7474/mcp \
  -H "Authorization: Bearer byte-gadgets-admin-local-dev" \
  -H "Content-Type: application/json" \
  -d '{...}'   # any MCP JSON-RPC request
```

## Stop

```sh
docker compose -f scripts/local_idp/docker-compose.yml down
```

## Notes

- `admin`/`admin`, the `canonic-mcp` client secret (`local-dev-secret`), and every test user
  password are local-dev-only values baked into `canonic-realm.json` — never use this realm
  export as a starting point for anything internet-reachable.
- `issuer_url: http://localhost:8080/realms/canonic` in `canonic.docker.yaml` looks odd for
  a Docker setup but is intentional: Keycloak's OIDC discovery document echoes back
  whatever host it was queried with, and that same document is what the *browser* gets
  redirected to for login — so it has to resolve on the browser's side, not just the
  container's. To make `localhost:8080` resolve to Keycloak from *inside* the `canonic`
  container too, `docker-compose.yml` puts it in Keycloak's network namespace
  (`network_mode: "service:keycloak"`) instead of giving it its own — that's also why
  `canonic`'s port 7474 is published from the `keycloak` service block, not its own.
  `base_url: http://localhost:7474` is unrelated: it's the host-published address the
  browser/MCP client redirects to for the *canonic* side of the OAuth dance.
