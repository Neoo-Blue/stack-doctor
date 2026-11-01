# Tools

Host-side helpers that run alongside stack-doctor. They use only the Python standard library, plus `curl` and `zstd` where
noted. Everything specific to your stack comes from the environment. Keep credentials there, never in these files.

| Tool | What it does |
|---|---|
| `probe_grab.py` | Grabs a handful of items only after checking that a release is really complete. Set `PROBE_GRAB_CMD` to it. |
| `indexer_watch.py` | Searches one indexer end to end from a timer, alerts when it dies, and catches up after it recovers. |
| `backfill_window.py` | Finds what an indexer posted during an outage and grabs only the monitored items that match. |
| `plex_start_bench.py` | Measures start time and rebuffering through Plex in a real client's read order. |
| `usenet.py` | Shared helper: reads AltMount's provider list and checks articles with STAT. |

## Configuration

| var | default | used by | meaning |
|---|---|---|---|
| `ARR_CONFIG_DIR` | `/opt/arr` | most | each `<dir>/<instance>/config.xml` holds that app's API key |
| `ARR_HOST` | `127.0.0.1` | most | where the arrs and Prowlarr listen |
| `ARR_PORTS` | `radarr=7878,sonarr=8989` | `indexer_watch` | arr instances to check |
| `PROWLARR_HOSTPORT` | `<ARR_HOST>:9696` | `probe_grab` | replaces the internal host in Prowlarr download links |
| `ALTMOUNT_CONFIG` | `/opt/altmount/config.yaml` | `usenet` | read for provider credentials, which stay in memory |
| `PROBE_FETCH_VIA_HOSTS` | *(none)* | `probe_grab` | comma-separated hosts only reachable through a prefix command |
| `PROBE_FETCH_VIA_CMD` | *(none)* | `probe_grab` | that prefix, e.g. `docker exec indexer` |
| `PROBE_GRAB_LOG` | `/var/log/probe_grab.log` | `probe_grab` | log file |
| `PROBE_GRAB_PAUSE_FILE` | `/run/probe_grab.pause` | `probe_grab` | while it exists, grabs wait |
| `WATCH_INDEXER_ID` | *(required)* | `indexer_watch`, `backfill_window` | the indexer's Prowlarr id |
| `WATCH_INDEXER_NAME` | *(none)* | `indexer_watch` | how the arrs name it, to clear their backoff after recovery |
| `WATCH_PROBE_QUERY` | `ubuntu` | `indexer_watch` | a query that always has results there |
| `WATCH_NOTIFY_CMD` | *(none)* | `indexer_watch` | gets the subject as an argument and the body on stdin; without it, alerts print |
| `WATCH_CAUSE_CMD` | *(none)* | `indexer_watch` | shell command whose output joins the alert |
| `WATCH_STATE_FILE` | `/var/lib/indexer-watch.json` | `indexer_watch` | outage state |
| `PLEX_URL`, `PLEX_TOKEN` | *(required)* | `plex_start_bench` | your Plex server |
| `PLEX_MOVIE_SECTION` | `1` | `plex_start_bench` | library section to sample |

## Examples

```sh
# Dry run: what would be grabbed for two episodes?
ARR_CONFIG_DIR=/opt/arr ./probe_grab.py sonarr 123 456 --dry-run

# Catch up after an outage (times are UTC)
WATCH_INDEXER_ID=1 ./backfill_window.py 2026-01-01T00:00:00 2026-01-02T12:00:00 --dry-run

# Compare a change: five cold starts, then five with a 40 MB warm
PLEX_URL=http://plex:32400 PLEX_TOKEN=... ./plex_start_bench.py cold 5
PLEX_URL=http://plex:32400 PLEX_TOKEN=... ./plex_start_bench.py warm 5 40
```

Run `indexer_watch.py` from a systemd timer every 15 minutes. It has to run as a user that can call `systemd-run`.
