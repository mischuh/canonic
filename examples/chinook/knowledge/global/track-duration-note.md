---
summary: "Track.Milliseconds stores raw duration — divide by 60000 for minutes, 1000 for seconds."
tags: [tracks, definitions, duration]
sl_refs:
  - chinook_pg.tracks.total_duration_ms
  - chinook_pg.tracks.avg_duration_ms
  - chinook_pg.tracks
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-06-18T00:00:00Z"
---

## Unit

`Track.Milliseconds` stores track duration as a raw integer count of **milliseconds**.

To convert for display:

| Unit | Expression |
|---|---|
| Seconds | `"Milliseconds" / 1000.0` |
| Minutes | `"Milliseconds" / 60000.0` |
| MM:SS   | `lpad(floor("Milliseconds"/60000)::text,2,'0') \|\| ':' \|\| lpad(floor(mod("Milliseconds",60000)/1000)::text,2,'0')` |

The `total_duration_ms` and `avg_duration_ms` measures on the `tracks` source return raw
milliseconds. Apply the conversion in your BI layer or in a derived expression when surfacing
duration to end users.

## Typical values in the Chinook dataset

- Shortest track: ~1,000 ms (1 second — likely a sound effect or intro clip)
- Typical song: 200,000–350,000 ms (3–6 minutes)
- Longest track: ~5,000,000 ms (~83 minutes — a live recording or audio book chapter)

A value above 3,600,000 ms (1 hour) almost always indicates a podcast episode, audio book, or
live concert recording, not a standard music track.
