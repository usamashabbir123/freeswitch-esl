FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Install minimal dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3-dev \
        python3-setuptools \
        python3-pip \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY domain_logger.py ./

ENV LOG_DIR=/var/logs/freeswitch
EXPOSE 8021

CMD ["python3", "domain_logger.py"]
