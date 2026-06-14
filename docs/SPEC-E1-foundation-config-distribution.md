# Spec — E1 Project Foundation, Config & Distribution

**Status:** Draft for review
**Parent:** PRD — Context Layer for Data Agents (FR-1; §5.2 layout, §5.6 operating modes; §9.1 Phase 0)
**Related:** SPEC E2 (connections), E5+E15 (`semantics/`, `contracts/`), E7+E8 (entry points)
**Last updated:** 2026-06-13

E1 is the ground the other epics stand on: how a project is created, configured, and laid out on disk, and how `canon` reaches the user's machine. It owns no business logic — it bootstraps the structure the other epics fill.

Phase markers: **[P0]** walking skeleton, **[P1]** v1 core, **[L]** later.

---

## 1. Scope

In scope:
- `canon.yaml` schema (project config, connections, LLM config).
- On-disk project layout and the committed-vs-local boundary.
- The `setup` wizard and resumability.
- Distribution & installation (npm / Homebrew / Docker), offline install.
- Version/upgrade and CLI↔daemon compatibility.
- Local state & secret storage in `.canon/`.

Out of scope (own specs): connection internals (E2), the three context surfaces' schemas (E5/E15, E6), CLI/MCP command behavior (E7/E8), ingestion (E4).

---

## 2. Project layout [P0]

Mirrors PRD §5.2. `canon` operates inside a project directory.

```text
my-project/
├── canon.yaml                  # project config + connections        (committed)
├── semantics/                  # executable definitions (E5)          (committed)
│   └── <connection-id>/*.yaml
├── knowledge/                  # business meaning (E6)                (committed)
│   ├── global/*.md
│   └── user/<user-id>/*.md
├── contracts/                  # authoritative rules (E15)            (committed)
│   ├── metrics/*.yaml
│   ├── guardrails/*.yaml
│   └── assertions/*.yaml
├── raw-sources/<connection-id>/   # ingest artifacts & reports        (committed [P1])
└── .canon/                     # local state + secrets                (git-ignored)
```

- **Committed:** `canon.yaml`, `semantics/`, `knowledge/`, `contracts/` (and `raw-sources/` once ingest exists).
- **Local-only:** everything under `.canon/` — secrets, daemon/runtime state, caches, event log (SPEC E2/E14). `canon setup` writes a `.gitignore` covering `.canon/` on init.
- A directory is recognized as a `canon` project iff a valid `canon.yaml` is present at its root.

---

## 3. `canon.yaml` schema [P0]

The one committed config file. Contains no secrets — those live in `.canon/` and are referenced indirectly.

```yaml
version: 1                       # [P0] config schema version (for migrations)
project:
  name: acme-analytics           # [P0]
  default_connection: warehouse_pg   # [P1] used when a query omits connection

connections:                     # [P0] non-secret connection descriptors
  - id: warehouse_pg             # [P0] referenced by semantics/<id>/…
    type: postgres               # [P0] selects the connector (E2)
    params:                      # non-secret connection params
      host: db.internal
      port: 5432
      database: analytics
      sslmode: require
    credentials_ref: env:CANON_PG_DSN   # [P0] indirection: env:… | keyring:… | file:.canon/secrets/…
    read_only_role: canon_ro     # [P1] documented least-privilege role (E2 §3)

llm:                             # [P0] runtime model config (SPEC scope: E10 owns details)
  provider: openai_compatible    # [P0] openai_compatible covers local (Ollama/vLLM/LM Studio) + hosted
  base_url: http://localhost:11434/v1   # [P0] local or hosted endpoint
  model: <model-id>              # [P0]
  api_key_ref: env:CANON_LLM_KEY # nullable for local
  tasks:                         # [P1] per-task model override (ingest vs reconcile)
    reconcile: <stronger-model>

embeddings:                      # [P1]
  provider: local
  model: <embedding-model>

telemetry:                       # [P0] off by default (PRD FR-14 / NFR)
  enabled: false
```

- **Secret indirection [P0]:** `credentials_ref`/`api_key_ref` never hold a secret value — they point to env var, OS keyring, or a git-ignored file under `.canon/`. Validation rejects a literal-looking secret in `canon.yaml`.
- **`version`** enables config migrations; an unknown/newer version is a clear error, not a silent partial parse.
- Schema is validated on every load; a malformed `canon.yaml` blocks all commands with a precise location.

---

## 4. Setup wizard & resumability [P0]

`canon setup` (PRD FR-1; entry behavior detailed in E7).
- Steps: name project → add first connection (delegates to E2 `add`+`test`) → configure LLM (provider/base_url/model) → fast initial schema bootstrap (E2 tier-1 introspection, optional in P0) → write `canon.yaml`, scaffold directories, write `.gitignore`.
- **Resumability [P0]:** progress is checkpointed in `.canon/setup-state`; re-running `canon setup` after an interruption resumes from the last completed step rather than restarting.
- **Idempotent:** running setup in an existing project enters a resume/connect/status menu (E7) instead of overwriting committed files.
- Connection test must pass before setup records the connection (inherits E2 lifecycle); a failing test does not write a broken connection into `canon.yaml`.

---

## 5. Distribution & installation [P0]

Three channels, one documented command each (PRD FR-1):

| Channel | Form | Use |
| --- | --- | --- |
| **npm** | scoped global package `@<scope>/canon` (bare `canon` is taken) | primary for dev machines |
| **Homebrew** | formula `canon` | macOS/Linux CLI users |
| **Docker** | image bundling CLI + daemon | the basis for CI/headless and air-gapped runs |

- **Binary:** the published command may be a distinct binary name if `canon` collides on a platform; documented per channel. (Brand/trademark resolution tracked in PRD.)
- **Offline / air-gapped install [P0]:** an install path with no outbound calls during install (e.g. the Docker image or a vendored tarball), matching the offline runtime mode (PRD FR-8). Documented explicitly.
- **What ships:** the CLI and the local MCP daemon are one distributable; local embeddings are an optional add-on install (E10), not bundled by default.

---

## 6. Versioning & upgrade [P0]

- `canon --version` reports the build.
- **CLI↔daemon compatibility:** checked when the daemon starts (SPEC E7/E8 §4.2); incompatible versions fail with a clear message rather than misbehaving.
- **`canon.yaml` migrations:** a newer config `version` than the binary understands is rejected with guidance to upgrade; an older version is auto-migratable with an explicit, logged migration step.
- Upgrade per channel is the channel's native mechanism (npm/brew/docker pull); no bespoke self-updater in v1.

---

## 7. Local state & secrets (`.canon/`) [P0]

- Holds: secrets (when `…_ref: file:.canon/secrets/…`), daemon runtime state, setup checkpoint, caches, event log (E14).
- Created with restrictive permissions; never committed (enforced by the generated `.gitignore`).
- Secret storage format and rotation is a shared open question (PRD §10); P0 supports env-var and OS-keyring indirection at minimum so secrets need not touch disk.

---

## 8. User stories & acceptance criteria

**S1 [P0] Initialize a project.**
- AC1: Given an empty directory, when I run `canon setup` and complete it, then `canon.yaml`, the four context directories, and a `.gitignore` covering `.canon/` exist, and the directory is recognized as a `canon` project.
- AC2: Secrets entered during setup are stored via indirection (env/keyring/`.canon`), never written into `canon.yaml`.

**S2 [P0] Resume an interrupted setup.**
- AC1: Given setup was interrupted after adding a connection, when I re-run `canon setup`, then it resumes at the next step and does not re-prompt completed ones.

**S3 [P0] Config validation.**
- AC1: Given a `canon.yaml` with a literal secret in a `*_ref` field, then load fails with a precise error pointing at the field.
- AC2: Given an unknown `version`, then load fails with an upgrade message; no partial parse.

**S4 [P0] Committed vs. local boundary.**
- AC1: After setup, `git status` shows `canon.yaml` and context directories as tracked and `.canon/` as ignored.

**S5 [P0] Install via each channel.**
- AC1: Each of npm (scoped), Homebrew, and Docker installs a working `canon` whose `--version` reports the build, per the documented command.
- AC2: The documented offline/air-gapped path installs with no outbound network calls.

**S6 [P0] Version compatibility.**
- AC1: Given a CLI and daemon of incompatible versions, when the daemon starts, then it fails with a clear version-mismatch message.

**S7 [P0] Re-running setup in an existing project is safe.**
- AC1: Given a valid project, when I run `canon setup`, then I get the resume/connect/status menu and no committed file is overwritten without confirmation.

**S8 [P1] Per-task LLM and default connection.**
- AC1: Given `llm.tasks.reconcile` set, then reconcile uses that model while other tasks use the default.
- AC2: Given `project.default_connection`, then a query omitting connection resolves against it.

---

## 9. Open questions (E1-specific)

- **Scoped npm name & binary name:** finalize the npm scope and whether the binary stays `canon` on all platforms (PRD brand/trademark item).
- **Secret storage format** in `.canon/` and rotation (shared with PRD §10 / E2).
- **Monorepo / multi-project:** is one `canon.yaml` per directory sufficient for v1, or is a multi-project workspace needed?
- **Config vs. CLI precedence:** when a flag and `canon.yaml` disagree (e.g. model), which wins and is it logged?
- **Docker daemon UX:** how a containerized daemon is reached by host-side agent clients (interacts with E8 §8 transport question).

---

## 10. Out of scope (this spec)

- Connection internals and the connector contract (E2).
- The `semantics/`/`knowledge/`/`contracts/` file schemas (E5/E15, E6).
- CLI/MCP command behavior and the daemon protocol (E7/E8).
- LLM/embeddings runtime details beyond the config block (E10).
- Ingestion and `raw-sources/` population (E4).
