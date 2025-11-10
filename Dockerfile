# -----------------------------
# Stage 1: Build the ESL Python wheel
# -----------------------------
FROM python:3.9-slim AS builder

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    swig \
    && rm -rf /var/lib/apt/lists/*

# Copy your local ESL Python module
COPY ./esl-python /opt/freeswitch-esl-python

# Build the wheel
WORKDIR /opt/freeswitch-esl-python
RUN python3 setup.py bdist_wheel

# -----------------------------
# Stage 2: Final runtime image
# -----------------------------
FROM python:3.9-slim

# Copy the built wheel from builder
COPY --from=builder /opt/freeswitch-esl-python/dist/*.whl /tmp/

# Install the ESL wheel
RUN pip install /tmp/*.whl && rm -rf /tmp/*.whl

# Copy your logger service code
WORKDIR /app
COPY . /app

# Run your FreeSWITCH logger service
CMD ["python3", "logger.py"]
