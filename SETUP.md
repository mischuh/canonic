# Release automation setup

This document lists the manual steps a maintainer needs to complete for
`.github/workflows/publish.yml` to fully succeed. None of these are required
for `release-please.yml` or the CI/commitlint/contract-schema-guard workflows.

## npm (`npm-publish` job)

Status: **scaffold only**. `package.json` and `bin/canon.js` exist so the
workflow structure is in place, but `bin/canon.js` is a stub that tells users
to install via `pip`/`uv` — there is no real binary-distribution mechanism
yet. SPEC-E1-foundation-config-distribution.md §5 leaves the npm scope and
distribution approach as an open question; `@mischuh/canon` is a placeholder
name, not a finalized one.

Required secret:

- `NPM_TOKEN` — an npm automation token with publish rights for the
  `@mischuh` org/scope. Create it at https://www.npmjs.com/ (Access Tokens →
  Generate New Token → Automation), then add it as a repository secret
  (Settings → Secrets and variables → Actions → New repository secret).

Why not OIDC/trusted publishing: npm's trusted-publisher (OIDC) flow requires
the package to already exist on the registry and have a trusted publisher
configured against it — it can't bootstrap a package that has never been
published. Once `@mischuh/canon` has a real first release, switch this job
to OIDC trusted publishing and drop `NPM_TOKEN`.

## Docker (`docker-publish` job)

Status: **fully functional**, no secrets required. It pushes to GHCR
(`ghcr.io/mischuh/canonic`) using the automatically-provided `GITHUB_TOKEN`.

One repo setting to confirm: Settings → Actions → General → Workflow
permissions → "Read and write permissions", so `GITHUB_TOKEN` is allowed to
push to GHCR.

## Homebrew (`homebrew-bump` job)

Status: **not implemented**. The job is a `TODO` placeholder. Implementing
it requires first creating a Homebrew tap/formula for `canonic`, which
doesn't exist yet.
