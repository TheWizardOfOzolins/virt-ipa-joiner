# Stage 1: Builder — Red Hat Hardened Python (Hummingbird OS)
FROM registry.access.redhat.com/hi/python:3.14-builder AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 0

# Native libs that python-freeipa links against at build time.
RUN dnf install -y \
        gcc \
        openldap-devel \
        cyrus-sasl-devel \
        openssl-devel && \
    dnf clean all && \
    rm -rf /var/cache/dnf

WORKDIR /app

# Build a self-contained venv so the runtime image (which has no package
# manager and no shell) just needs to COPY a directory.
RUN python3 -m venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Stage 2: Runtime — distroless hardened image, no shell, no dnf
FROM registry.access.redhat.com/hi/python:3.14

# APP_VERSION must be redeclared here — ARG/ENV from the builder stage
# do not carry across multi-stage FROM boundaries.
ARG APP_VERSION=0.0.0
ENV APP_VERSION=$APP_VERSION \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder --chown=65532:65532 /app/.venv /app/.venv
COPY --chown=65532:65532 app ./app

USER 65532

EXPOSE 8443

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8443", "--ssl-keyfile", "/var/run/secrets/serving-cert/tls.key", "--ssl-certfile", "/var/run/secrets/serving-cert/tls.crt"]
