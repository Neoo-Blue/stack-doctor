#!/usr/bin/env python3
"""Notice a dead usenet indexer before it costs days of missed releases. Run it from a timer: it searches one indexer
through Prowlarr end to end, alerts after two failed runs and every 24 h after that, and on recovery clears the arrs'
own backoff, reports, and starts backfill_window.py for the outage. Prowlarr and the arrs only log these failures."""
import datetime as D, json, os, shlex, re, subprocess, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ARR_DIR = os.environ.get("ARR_CONFIG_DIR", "/opt/arr")
ARR_HOST = os.environ.get("ARR_HOST", "127.0.0.1")
STATE = os.environ.get("WATCH_STATE_FILE", "/var/lib/indexer-watch.json")
INDEXER = int(os.environ["WATCH_INDEXER_ID"])                    # the indexer's Prowlarr id
NAME = os.environ.get("WATCH_INDEXER_NAME", "")                  # how the arrs name it, to clear their backoff
PROBE = os.environ.get("WATCH_PROBE_QUERY", "ubuntu")            # any query that always has results there
NOTIFY = shlex.split(os.environ.get("WATCH_NOTIFY_CMD", ""))     # gets the subject as an argument, the body on stdin
CAUSE = os.environ.get("WATCH_CAUSE_CMD", "")                    # optional shell command, its output joins the alert
REPEAT = 86400
ENV_PREFIXES = ("ARR_", "ALTMOUNT_", "PROBE_", "PROWLARR_", "WATCH_")   # what the backfill needs from this environment
PORTS = dict(p.split("=") for p in os.environ.get("ARR_PORTS", "radarr=7878,sonarr=8989").split(","))


def key(n):
    return re.search(r"<ApiKey>(.*?)</ApiKey>", open(f"{ARR_DIR}/{n}/config.xml").read())[1]


def api(port, k, path, ver="v3", data=None):
    r = urllib.request.Request(f"http://{ARR_HOST}:{port}/api/{ver}/{path}", method="POST" if data is not None else "GET",
                               data=None if data is None else json.dumps(data).encode(),
                               headers={"X-Api-Key": k, "Content-Type": "application/json"})
    raw = urllib.request.urlopen(r, timeout=120).read()
    return json.loads(raw) if raw else None


def probe():
    """None when healthy, else what is wrong."""
    try:
        q = urllib.parse.quote(PROBE)
        rs = api(9696, key("prowlarr"), f"search?type=search&indexerIds={INDEXER}&limit=5&query={q}", "v1")
        return None if rs else "search returned nothing"
    except Exception as ex:
        return f"Prowlarr search failed: {getattr(ex, 'code', ex)}"


def cause():
    if not CAUSE:
        return ""
    return subprocess.run(CAUSE, shell=True, capture_output=True, text=True, timeout=60).stdout.strip()


def clear_arr_backoff():
    if not NAME:
        return
    for n, port in PORTS.items():
        k = key(n)
        if any(NAME in h.get("message", "") and "unavailable" in h["message"] for h in api(port, k, "health")):
            for i in api(port, k, "indexer"):
                if NAME in i["name"]:
                    api(port, k, "indexer/test", data=i)


def notify(subject, body):
    if NOTIFY:
        subprocess.run(NOTIFY + [subject], input=body, text=True, check=True)
    else:
        print(subject, body, sep="\n")


def main():
    st = json.load(open(STATE)) if os.path.exists(STATE) else {}
    now, bad = time.time(), probe()
    if bad:
        st.setdefault("since", now)
        st["fails"] = st.get("fails", 0) + 1
        if st["fails"] >= 2 and now - st.get("alerted", 0) >= REPEAT:
            since = time.strftime("%F %H:%M", time.localtime(st["since"]))
            notify("Indexer DOWN", f"Down since {since}.\n{bad}\n{cause()}\n\nReleases posted meanwhile are caught up after recovery.")
            st["alerted"] = now
    else:
        clear_arr_backoff()
        if st.get("alerted"):
            a, b = [D.datetime.fromtimestamp(t, D.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") for t in (st["since"] - 3600, now + 300)]
            passed = [f"--setenv={k}={v}" for k, v in os.environ.items() if k.startswith(ENV_PREFIXES)]
            subprocess.run(["systemd-run", f"--unit=indexer-backfill-{int(now)}", "--collect", "--property=Nice=10", *passed,
                            os.path.join(HERE, "backfill_window.py"), a, b], check=True)
            notify("Indexer recovered", f"Back at {time.strftime('%F %H:%M')}. Backfilling {a} to {b} UTC.")
        st = {}
    json.dump(st, open(STATE, "w"))


if __name__ == "__main__":
    main()
