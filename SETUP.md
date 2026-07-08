# Release automation setup

This document lists the manual steps a maintainer needs to complete for
`.github/workflows/publish.yml` to fully succeed. None of these are required
for `release-please.yml` or the CI/commitlint/contract-schema-guard workflows.

## PyPI (`pypi-publish` job)

Status: **functional once the one-time PyPI-side setup below is done** — no
repository secret required. Distribution is Python-only (uv/PyPI primary,
pip fallback, Docker for CI/air-gapped); npm and Homebrew were dropped as
channels (see `specs/AMENDMENT-python-only-distribution.md`).

The job publishes via `uv build` + `uv publish`, authenticating with PyPI
Trusted Publishing (OIDC) — no `PYPI_TOKEN` needed. Unlike npm's
trusted-publisher flow, PyPI supports registering a **pending** trusted
publisher for a project name that doesn't exist yet, so there's no
bootstrap chicken-and-egg problem here.

One-time setup (maintainer, on pypi.org, before the next tag push):

1. Create a GitHub Environment named `pypi` in this repo (Settings →
   Environments → New environment).
2. On PyPI, add a pending trusted publisher for project name `canonic`:
   owner `mischuh`, repository `canonic`, workflow `publish.yml`,
   environment `pypi`.

Once both are in place, the first tag-triggered release creates the
`canonic` project on PyPI automatically.

## Docker (`docker-publish` job)

Status: **fully functional**, no secrets required. It pushes to GHCR
(`ghcr.io/mischuh/canonic`) using the automatically-provided `GITHUB_TOKEN`.

One repo setting to confirm: Settings → Actions → General → Workflow
permissions → "Read and write permissions", so `GITHUB_TOKEN` is allowed to
push to GHCR.
