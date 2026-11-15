# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SECRETS_DIR=/secrets \
    PORT=8790

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY secret_drop ./secret_drop

# 568 is the TrueNAS "apps" user. Override with `user:` in compose.
RUN mkdir -p /secrets && chown 568:568 /secrets
USER 568:568

EXPOSE 8790
VOLUME ["/secrets"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8790'), timeout=2)"]

LABEL org.opencontainers.image.title="secret-drop" \
      org.opencontainers.image.description="Tiny self-hosted secret drop box with a phone UI and an MCP server that never reveals values" \
      org.opencontainers.image.source="https://github.com/tduarte/tinysecret" \
      org.opencontainers.image.licenses="MIT"

CMD ["python", "-m", "secret_drop"]
