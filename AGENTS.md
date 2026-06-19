# Agent Notes

This project is a pure-Python package (no third-party dependencies).

## How to run

```bash
python3 -m doctor
```

## How to run tests

```bash
python3 -m unittest discover -s tests -v
```

## Quick smoke test

```bash
ENABLE_QUEUE=false ENABLE_UI=true DOCTOR_UI_PORT=12345 DOCTOR_STATE_FILE=/tmp/doctor_state.json timeout 3 python3 -m doctor
```

## How to deploy

- Docker: `COPY doctor /app/doctor` and `ENTRYPOINT ["python3", "-m", "doctor"]`.
- Host systemd: copy the `doctor/` package directory, set `WorkingDirectory` to that directory, and run `python3 -m doctor`.
