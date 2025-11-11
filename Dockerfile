# Multi-stage build for efficient final image
FROM python:3.12-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    ca-certificates \
    g++ \
    gcc \
    git \
    libpcre2-dev \
    python3-dev \
    python3-pip \
    python3-setuptools \
    swig \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip, setuptools, wheel
RUN python3 -m pip install --upgrade pip setuptools wheel

# Copy local ESL source
WORKDIR /build
COPY esl-python/ .

# Build and install python-ESL from source
RUN python3 setup.py build_ext --inplace && \
    python3 setup.py install

# Final runtime image
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies including development libraries for dynamic loading
RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    zlib1g \
    && rm -rf /var/lib/apt/lists/* && \
    mkdir -p /var/log/freeswitch-logs /var/log/freeswitch && \
    useradd -m -u 1000 logger && \
    chown -R logger:logger /var/log/freeswitch-logs /var/log/freeswitch

# Copy Python site-packages from builder (includes ESL and all compiled extensions)

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

# Copy the application
WORKDIR /app
COPY --chown=logger:logger logger.py .
COPY --chown=logger:logger healthcheck.py .
COPY --chown=logger:logger requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt && \
    chown -R logger:logger /app

USER logger

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python3 /app/healthcheck.py

CMD ["python3", "/app/logger.py"]

