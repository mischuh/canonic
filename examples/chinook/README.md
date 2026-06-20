# Canon Chinook demo

An end-to-end Canon project built on the [Chinook](https://github.com/lerocha/chinook-database)
digital music store dataset: one Postgres connection, an eleven-source schema (two facts, seven
dimensions, one bridge), three canonical metrics, and three enforced guardrails.

Covers the complete **Phase 1 loop**: ingest bootstraps context from a real stack, the MCP
server gives agents both executable definitions and business meaning, and `canon eval` tracks
accuracy.

## Phase 1 loop

```
canon ingest --bootstrap          # 1. bootstrap: introspect Postgres → write semantics/*.yaml
canon mcp start                   # 2. serve: agents call query() + search_knowledge() together
canon eval baseline \             # 3. track: measure grain-inference accuracy on the live schema
  --candidates candidates.yaml \
  --dataset eval/grain_cases.jsonl
```

Each step proves one Phase 1 exit criterion:

| Step | Criterion |
| --- | --- |
| `canon ingest --bootstrap` | Bootstraps context from a real stack |
| `query()` + `search_knowledge()` | Agents get both executable definitions and business meaning |
| `canon eval baseline` | Accuracy is tracked |

## What's in here

```
canon.yaml                              ← project config + Postgres (chinook_pg) + dbt connection
setup.sql                               ← Chinook DB dump: 11 tables, ~350 artists, ~3500 tracks,
                                          412 customers, 412 invoices
dbt/manifest.json                       ← E3 definition connector: compiled dbt manifest for the
                                          revenue path (invoices, lines, tracks, customers) — offline
docs/notion-pages/
  revenue-definition.md                 ← Canon Type: definition — prose for the revenue metric
  tracks-sold-definition.md             ← Canon Type: definition — prose for tracks_sold
  track-price-caveat.md                 ← Canon Type: caveat   — list price vs. sale price trap
  invoice-line-fanout-caveat.md         ← Canon Type: caveat   — cross-grain join trap
candidates.yaml                         ← local model candidates for canon eval baseline
eval/grain_cases.jsonl                  ← labeled grain-inference cases for the Chinook schema
semantics/chinook_pg/
  invoices.yaml                         ← fact: total_revenue / invoice_count / avg_invoice_value
  invoice_lines.yaml                    ← fact: line_revenue / tracks_sold / line_count
  customers.yaml                        ← dim: country, city, company — join target for invoices
  tracks.yaml                           ← dim: name, genre, media_type — join target for invoice_lines
  albums.yaml                           ← dim: title — join target for tracks
  artists.yaml                          ← dim: name — join target for albums
  genres.yaml                           ← dim: name — join target for tracks
  media_types.yaml                      ← dim: name (AAC, MP3, MPEG…) — join target for tracks
  employees.yaml                        ← dim: title, hire_date — support rep join target for customers
  playlists.yaml                        ← dim: playlist_name — one row per named playlist
  playlist_tracks.yaml                  ← bridge: PlaylistId × TrackId — track membership across playlists
contracts/metrics/
  revenue.yaml                          ← canonical binding: revenue → invoices.total_revenue
  invoice-count.yaml                    ← canonical binding: invoice_count → invoices.invoice_count
  tracks-sold.yaml                      ← canonical binding: tracks_sold → invoice_lines.tracks_sold
contracts/guardrails/
  invoice-lines-positive-quantity.yaml  ← mandatory_filter: Quantity > 0 on invoice_lines
  use-sale-price-not-list-price.yaml    ← advisory: use InvoiceLine.UnitPrice, not Track.UnitPrice
  no-playlist-join-in-revenue.yaml      ← forbidden_join: PlaylistTrack causes fanout in revenue queries
knowledge/global/
  revenue-definition.md                 ← usage_mode: definition — what total_revenue means
  tracks-sold-definition.md             ← usage_mode: definition — what tracks_sold means + grain
  invoice-line-fanout-caveat.md         ← usage_mode: caveat — line-item fanout trap
  track-price-caveat.md                 ← usage_mode: caveat — list price vs sale price distinction
  revenue-reporting-policy.md           ← usage_mode: policy — InvoiceDate attribution, geo rules
  track-duration-note.md                ← usage_mode: definition — Milliseconds unit + conversion
```

## Schema overview

```
invoices ──── invoice_lines ──── tracks ──── albums ──── artists
    │                │                ├───── genres
    │                │                └───── media_types
    │                └──────────── playlist_tracks ──── playlists
    └──── customers ──── employees
```

The two fact tables (`invoices` and `invoice_lines`) sit at different grains — do not join
them in the same query. See `knowledge/global/invoice-line-fanout-caveat.md`.

## E3 connectors — definitions beyond raw introspection

Postgres introspection (E2) discovers what tables exist; the **E3 definition connector** reads
what they *mean*. This demo ships a compiled dbt manifest at [`dbt/manifest.json`](dbt/manifest.json)
covering the revenue path (`Invoice`, `InvoiceLine`, `Track`, `Customer`) with measures
(`total_revenue`, `tracks_sold`), entities, and joins. It is wired into [`canon.yaml`](canon.yaml)
as a second connection — **no database, no credentials**:

```yaml
connections:
  - id: chinook_dbt
    type: dbt
    params:
      manifest_path: dbt/manifest.json   # relative to canon.yaml
    # no credentials_ref — a manifest is a local file, not a guarded endpoint
```

`canon ingest` reconciles it into reviewable semantic proposals entirely from the file — **no
Postgres, no LLM**:

```sh
canon ingest --connection chinook_dbt --dry-run
# ## Decisions
# - add: 4            ← one proposal per dbt model (Invoice, InvoiceLine, Track, Customer)
#
# ### semantics/chinook_dbt/InvoiceLine.yaml (add)
# - provenance: inferred, confidence: 1.0
# +joins:
# +- to: Invoice
# +  on: InvoiceLine.InvoiceId = Invoice.InvoiceId
# +  relationship: many_to_one          ← reconstructed from the manifest's FK constraints
```

Every record lands at acquisition tier `modeling`, which **outranks** raw `live` introspection
on semantics during reconciliation — a hand-modeled grain or additivity beats a guess from raw
columns, and a genuine disagreement surfaces as a contradiction rather than a silent merge.
Like every E3 connector, the dbt source advertises no `run_read_only_sql` capability: it is read
for meaning, never queried for data (the no-execution invariant, SPEC-E3 §2).

This demo also ships four sample Notion page sources in [`docs/notion-pages/`](docs/notion-pages/)
showing what to write in a Notion workspace before pointing Canon at it. Each file shows the
`Canon Type` and `Canon Topics` page properties the Notion connector reads, followed by the prose
that becomes `DocEvidence.body`.

For the full E3 connector reference — including the **evidence** connectors (Notion →
`DocEvidence`, Metabase / Looker → `UsageEvidence`, BI usage as candidates-only per FR-13) — see
the [ecommerce demo's E3 section](../ecommerce/README.md#e3-connectors--definitions--evidence-beyond-the-primary-source).

## Prerequisites

- Python ≥ 3.13, Canon installed (`pip install -e ../..` from this directory)
- A Postgres database you can write to (local Docker, Neon free tier, etc.)

## Setup

**1. Load the Chinook database:**

```sh
export CANON_PG_PASSWORD=postgres   # password for the postgres user
psql "postgres://postgres:${CANON_PG_PASSWORD}@localhost:5432/postgres" < setup.sql
```

The dump creates the `chinook` schema and populates all tables.

**2. Verify the project is recognised:**

```sh
cd examples/chinook   # ← must run canon commands from here
canon status
# Canon project: Chinook (version 1)
# Root: /path/to/examples/chinook
# Connection: chinook_pg (postgres)
```

## Start the MCP server

**Stdio — for Claude Code / Cursor (the MCP client owns the process):**

```sh
canon mcp start
```

Add this to your Claude Code MCP config (`~/.claude.json` or the project `.claude.json`):

```json
{
  "mcpServers": {
    "canon": {
      "command": "canon",
      "args": ["mcp", "start"],
      "cwd": "/absolute/path/to/examples/chinook"
    }
  }
}
```

## Sample queries (via MCP)

```
# Total revenue by billing country
query(source="invoices", measures=["total_revenue"], dimensions=["billing_country"])

# Top genres by tracks sold
query(source="invoice_lines", measures=["tracks_sold"],
      dimensions=["track_id"], joins=["tracks", "genres"])

# Average invoice value by year
query(source="invoices", measures=["avg_invoice_value"],
      dimensions=["invoice_date"], time_grain="year")

# Support rep performance: revenue per rep
query(source="invoices", measures=["total_revenue", "invoice_count"],
      joins=["customers", "employees"], dimensions=["employee_title"])
```

## Event log & observability (`canon report`)

Every query served by the MCP server or CLI appends a `served_answer` event to
`.canon/events.jsonl` (local, git-ignored). Every `canon ingest` run appends
`reconcile_decision` events to the same file. Both kinds share one unified log.

```sh
canon report
# canon report  (telemetry: off)
#
# answers:        17  (2026-06-01T… → 2026-06-19T…)
# latency:        p50 420ms  p95 1800ms  min 200ms  max 2300ms  avg 550ms
# bytes scanned:  total 891,234  …
# stale answers:  0
# guardrail hits: 12
```

```sh
canon --json report          # machine-readable summary
canon report --last 50       # restrict to the most recent 50 events
```

Events log SHA-256 hashes of the request and compiled SQL, latency, bytes scanned, and
which guardrail IDs fired — never SQL text, result rows, or user input.

## Privacy & air-gapped mode

`telemetry.enabled: false` (the default) keeps the event log purely local. To harden
this at config-load time so it can never be enabled accidentally:

```yaml
# canon.yaml
runtime:
  air_gapped: true
```

With `air_gapped: true` Canon also rejects any LLM `base_url` that resolves outside
loopback (or an explicit `allow_cidrs` range) and any secret ref that uses a remote
scheme. The daemon refuses to start mis-configured — `canon status` confirms load success.

## Key design notes

### PascalCase columns

Chinook uses quoted PascalCase identifiers (`"InvoiceId"`, `"BillingCountry"`, etc.) — a legacy
of its origin as a cross-database demo. All `measure.expr` fields use double-quoted column names
accordingly. Dimension `column` references use bare PascalCase; the semantic layer quotes them
when generating SQL.

### Invoice.Total vs InvoiceLine aggregation

`Invoice.Total` is the pre-computed sum of its line items. `invoices.total_revenue` aggregates
this column directly (`sum("Total")`), while `invoice_lines.line_revenue` recomputes from the
line-item grain (`sum("UnitPrice" * "Quantity")`). Both should produce the same number when
queried at the invoice level — a discrepancy signals a data-integrity issue.

### Playlist bridge table

`playlist_tracks` is a many-to-many bridge: one track can appear in multiple playlists. The
`no-playlist-join-in-revenue` guardrail prevents it from being joined into revenue queries —
every invoice line item would be duplicated once per playlist the purchased track belongs to.

For playlist-content analysis (`tracks_per_playlist`, `avg_track_duration_by_playlist`), query
`playlist_tracks` directly and join to `playlists` and `tracks`. Never mix this path with
`invoice_lines` in the same query.
