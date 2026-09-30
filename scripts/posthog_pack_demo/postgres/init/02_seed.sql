-- Synthetic but coherent PostHog-shaped demo data for team_id = 1, spread over the last
-- 30 days, sized to give every metric the pack installs a non-trivial answer:
--   - 30 persons total, 3 tagged with an internal "@yourcompany.com" email (properties
--     and every event's person_properties) to exercise exclude-internal-traffic.
--   - every person has a "user_signed_up" event (feeds new_users).
--   - about a third of the 27 external persons also have a "report_created" event
--     (feeds activated_users -> a non-trivial activation_rate).
--   - "$pageview"/"dashboard_viewed" noise events on top (feeds active_users, and the
--     "$pageview" one is excluded by the "$"-prefix filter in the pack's choose_from
--     query if this demo is later re-run through the interactive wizard).
SELECT setseed(0.42);

WITH persons_raw AS (
    SELECT
        i,
        'user-' || lpad(i::text, 3, '0') AS distinct_id,
        gen_random_uuid() AS person_id,
        (i <= 3) AS internal,
        CASE WHEN i <= 3
            THEN 'teammate' || i || '@yourcompany.com'
            ELSE 'user' || lpad(i::text, 3, '0') || '@example.com'
        END AS email,
        now() - ((30 - i) || ' days')::interval - (random() * interval '12 hours') AS signed_up_at
    FROM generate_series(1, 30) AS i
)
INSERT INTO posthog_exports.persons
    (team_id, distinct_id, person_id, properties, person_distinct_id_version, person_version, created_at)
SELECT
    1,
    distinct_id,
    person_id,
    jsonb_build_object('email', email, 'internal', internal),
    0,
    0,
    signed_up_at
FROM persons_raw;

-- Every person: a signup event.
INSERT INTO posthog_exports.events (uuid, event, properties, person_properties, distinct_id, team_id, ip, timestamp)
SELECT
    gen_random_uuid(),
    'user_signed_up',
    '{}'::jsonb,
    jsonb_build_object('email', p.properties->>'email'),
    p.distinct_id,
    p.team_id,
    '203.0.113.' || (row_number() OVER (ORDER BY p.distinct_id))::text,
    p.created_at
FROM posthog_exports.persons p;

-- About a third of external persons: an activation event a few days after signup.
INSERT INTO posthog_exports.events (uuid, event, properties, person_properties, distinct_id, team_id, ip, timestamp)
SELECT
    gen_random_uuid(),
    'report_created',
    jsonb_build_object('report_type', 'weekly_summary'),
    jsonb_build_object('email', p.properties->>'email'),
    p.distinct_id,
    p.team_id,
    '203.0.113.' || (row_number() OVER (ORDER BY p.distinct_id))::text,
    p.created_at + (random() * interval '4 days') + interval '1 hour'
FROM posthog_exports.persons p
WHERE (p.properties->>'internal')::boolean = false
  AND (regexp_replace(p.distinct_id, '\D', '', 'g'))::int % 3 = 0;

-- Everyone (including the internal team): a handful of pageview/dashboard-view noise
-- events across the window, so active_users reflects ongoing usage, not just signup day.
INSERT INTO posthog_exports.events (uuid, event, properties, person_properties, distinct_id, team_id, ip, timestamp)
SELECT
    gen_random_uuid(),
    (ARRAY['$pageview', 'dashboard_viewed'])[1 + floor(random() * 2)::int],
    '{}'::jsonb,
    jsonb_build_object('email', p.properties->>'email'),
    p.distinct_id,
    p.team_id,
    '203.0.113.' || (row_number() OVER (ORDER BY p.distinct_id, gs))::text,
    p.created_at + (gs || ' days')::interval + (random() * interval '10 hours')
FROM posthog_exports.persons p
CROSS JOIN generate_series(1, 3) AS gs
WHERE p.created_at + (gs || ' days')::interval <= now();
