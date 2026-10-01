-- Schema matching PostHog's documented Postgres batch-export format (see
-- https://posthog.com/docs/cdp/batch-exports/postgres and canonic-packs'
-- packs/posthog/mappings/postgres.yaml / models/{events,persons}.yaml), so the
-- canonic-packs "posthog" pack installs against this database unmodified.
--
-- One deliberate deviation from PostHog's real export: `properties`/`person_properties`
-- are `json` here, not `jsonb`. The pack's exclude-internal-traffic guardrail filter uses
-- Postgres's native `->>` jsonb operator (`person_properties->>'email'`); canonic's query
-- compiler round-trips that raw filter text through sqlglot before executing it, which
-- re-renders `->>` as `JSON_EXTRACT_PATH_TEXT(...)` — a function Postgres only defines for
-- `json`, not `jsonb` (`json_extract_path_text(jsonb, unknown) does not exist`). This is a
-- real gap between the compiler and a real jsonb-typed PostHog export, not just a demo
-- quirk; using `json` here keeps this demo runnable without papering over that gap
-- elsewhere. ph_events.yaml/ph_persons.yaml (canonic-packs) both declare these columns as
-- canonic's semantic `json` type either way, so nothing about the installed pack content
-- changes.

CREATE SCHEMA IF NOT EXISTS posthog_exports;

CREATE TABLE posthog_exports.events (
    uuid              uuid PRIMARY KEY,
    event             text NOT NULL,
    properties        json,
    person_properties json,
    distinct_id       text NOT NULL,
    team_id           integer NOT NULL,
    ip                text,
    timestamp         timestamptz NOT NULL
);

CREATE TABLE posthog_exports.persons (
    team_id                    integer NOT NULL,
    distinct_id                text NOT NULL,
    person_id                  uuid NOT NULL,
    properties                 json,
    person_distinct_id_version integer,
    person_version             integer,
    created_at                 timestamptz NOT NULL,
    PRIMARY KEY (team_id, distinct_id)
);

CREATE INDEX ON posthog_exports.events (distinct_id, team_id);
CREATE INDEX ON posthog_exports.events (event);
CREATE INDEX ON posthog_exports.events (timestamp);
