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
# docker-ce-cli lets a restart hook control local containers via a bind-mounted /var/run/docker.sock.
# Runs as root so a bind-mounted /data (and an optional rw /mnt/library for the
# janitor) is always writable regardless of host ownership.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/* /etc/apt/keyrings /etc/apt/sources.list.d/docker.list \
    && mkdir -p /data
VOLUME /data

# webhook port (event mode) + web dashboard (ENABLE_UI)
EXPOSE 8088 12345

ENTRYPOINT ["python3", "-m", "doctor"]
