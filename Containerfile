FROM registry.access.redhat.com/ubi9/python-312:latest

# Switch to root to install system dependencies
USER 0

ARG APP_VERSION=0.0.0
ENV APP_VERSION=$APP_VERSION

# Install system dependencies required for python-freeipa (LDAP/SASL)
RUN dnf install -y \
    gcc \
    openldap-devel \
    cyrus-sasl-devel \
    openssl-devel && \
    dnf clean all && \
    rm -rf /var/cache/dnf

# Switch back to the default non-root user provided by the image
# UBI images typically use user 1001
USER 1001

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/opt/app-root/src

# Set work directory (Standard for UBI Python images)
WORKDIR $APP_HOME

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    rm -f requirements.txt

# Copy the application code
COPY app ./app

# Expose 8443 (Standard for secured webhooks)
EXPOSE 8443

# Run Uvicorn with SSL enabled
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8443", "--ssl-keyfile", "/var/run/secrets/serving-cert/tls.key", "--ssl-certfile", "/var/run/secrets/serving-cert/tls.crt"]
