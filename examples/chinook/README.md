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
canon.yaml                              ← project config + Postgres connection (chinook_pg)
setup.sql                               ← Chinook DB dump: 11 tables, ~350 artists, ~3500 tracks,
                                          412 customers, 412 invoices
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
