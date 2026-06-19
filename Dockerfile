FROM python:3.12-slim

LABEL org.opencontainers.image.title="stack-doctor" \
      org.opencontainers.image.description="Auto-detect and fix recurring Sonarr/Radarr stuck-queue issues (cron or webhook)" \
      org.opencontainers.image.source="https://github.com/Neoo-Blue/stack-doctor" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    DOCTOR_STATE_FILE=/data/state.json

WORKDIR /app
COPY doctor /app/doctor

# The doctor package uses only the Python standard library. openssh-client lets a
# restart hook reach a *host* service (e.g. DECYPHARR_RESTART_CMD="ssh root@host systemctl restart decypharr").
# Runs as root so a bind-mounted /data (and an optional rw /mnt/library for the
# janitor) is always writable regardless of host ownership.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data
VOLUME /data

# webhook port (event mode) + web dashboard (ENABLE_UI)
EXPOSE 8088 12345

ENTRYPOINT ["python3", "-m", "doctor"]
