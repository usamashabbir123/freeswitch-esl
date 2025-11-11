# FreeSWITCH Logger - Deployment Guide

## Pre-Deployment Checklist

- [ ] Docker Engine 24.0+ installed
- [ ] Docker Compose v2.0+ installed
- [ ] FreeSWITCH server with ESL enabled
- [ ] ESL port (default 8021) accessible
- [ ] ESL authentication credentials
- [ ] Disk space for logs (recommend 100GB+)
- [ ] Network connectivity to FreeSWITCH server

## Environment Configuration

### 1. Create `.env` File

Copy the template and customize:

```bash
cp .env.template .env
```

Edit `.env` with your values:

```env
# FreeSWITCH Connection
ESL_HOST=your-freeswitch-server.example.com
ESL_PORT=8021
ESL_PASSWORD=ClueCon

# Log Storage
LOG_DIR=/var/log/freeswitch-logs
LOG_LEVEL=INFO

# Performance (tune based on your load)
RECONNECT_DELAY=5
BUFFER_FLUSH_INTERVAL=5
FILE_ROTATION_SIZE=104857600  # 100MB - adjust for your disk space
MAX_FILE_DESCRIPTORS=50        # Increase if you have 50+ domains

# Docker
HOST_LOG_PATH=./logs  # Where logs appear on host machine
```

### 2. Important Configuration Notes

#### LOG_LEVEL
- `DEBUG` - Very verbose, shows all parsing attempts (not recommended for production)
- `INFO` - Normal operation, shows key events and metrics
- `WARNING` - Only shows issues
- `ERROR` - Only shows errors

#### FILE_ROTATION_SIZE
Default: 104857600 (100MB)
- Smaller (e.g., 10MB): More frequent rotations, more disk I/O
- Larger (e.g., 500MB): Fewer rotations, but large files
- Recommendation: 50-200MB depending on your log volume

#### MAX_FILE_DESCRIPTORS
Default: 50
- If you have < 50 domains: Keep at 50
- If you have 50-100 domains: Set to 100
- If you have 100+ domains: Set to 256 or increase system limit

---

## Deployment Methods

### Method 1: Docker Compose (Recommended)

```bash
# Build the image
docker-compose build

# Start the service
docker-compose up -d

# Monitor
docker-compose logs -f freeswitch-logger

# Health check
docker-compose exec freeswitch-logger python3 /app/healthcheck.py

# Stop
docker-compose stop
```

### Method 2: Docker CLI

```bash
# Build image
docker build -t freeswitch-logger:latest .

# Run container
docker run -d \
  --name freeswitch-logger \
  --restart unless-stopped \
  -e ESL_HOST=your-freeswitch-server \
  -e ESL_PORT=8021 \
  -e ESL_PASSWORD=ClueCon \
  -e LOG_DIR=/var/log/freeswitch-logs \
  -v freeswitch-logs:/var/log/freeswitch-logs \
  freeswitch-logger:latest

# Monitor
docker logs -f freeswitch-logger

# Health check
docker exec freeswitch-logger python3 /app/healthcheck.py
```

### Method 3: Kubernetes

#### Create ConfigMap
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: freeswitch-logger-config
  namespace: default
data:
  ESL_HOST: "freeswitch.default.svc.cluster.local"
  ESL_PORT: "8021"
  LOG_DIR: "/var/log/freeswitch-logs"
  LOG_LEVEL: "INFO"
```

#### Create Secret
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: freeswitch-logger-secret
  namespace: default
type: Opaque
stringData:
  ESL_PASSWORD: "ClueCon"
```

#### Create Deployment
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: freeswitch-logger
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: freeswitch-logger
  template:
    metadata:
      labels:
        app: freeswitch-logger
    spec:
      containers:
      - name: logger
        image: freeswitch-logger:latest
        imagePullPolicy: IfNotPresent
        
        envFrom:
        - configMapRef:
            name: freeswitch-logger-config
        env:
        - name: ESL_PASSWORD
          valueFrom:
            secretKeyRef:
              name: freeswitch-logger-secret
              key: ESL_PASSWORD
        
        resources:
          requests:
            memory: "64Mi"
            cpu: "100m"
          limits:
            memory: "256Mi"
            cpu: "500m"
        
        livenessProbe:
          exec:
            command:
            - python3
            - /app/healthcheck.py
          initialDelaySeconds: 30
          periodSeconds: 30
          timeoutSeconds: 10
          failureThreshold: 3
        
        readinessProbe:
          exec:
            command:
            - python3
            - /app/healthcheck.py
          initialDelaySeconds: 10
          periodSeconds: 10
          timeoutSeconds: 5
          failureThreshold: 2
        
        volumeMounts:
        - name: logs
          mountPath: /var/log/freeswitch-logs
      
      volumes:
      - name: logs
        persistentVolumeClaim:
          claimName: freeswitch-logs-pvc
      
      restartPolicy: Always
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: freeswitch-logs-pvc
  namespace: default
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 100Gi
  storageClassName: standard
```

Deploy:
```bash
kubectl apply -f configmap.yaml
kubectl apply -f secret.yaml
kubectl apply -f deployment.yaml

# Monitor
kubectl logs -f deployment/freeswitch-logger
kubectl exec -it deployment/freeswitch-logger -- python3 /app/healthcheck.py
```

---

## Verification Steps

### 1. Check Container Status
```bash
docker-compose ps
# or
docker ps | grep freeswitch-logger
```

Expected output:
```
NAME                    STATUS              PORTS
freeswitch-logger       Up About a minute
```

### 2. Run Health Check
```bash
docker-compose exec freeswitch-logger python3 /app/healthcheck.py
```

Expected output:
```
✓ Health check OK: All systems operational
```

### 3. Verify Log Files
```bash
docker-compose exec freeswitch-logger ls -lah /var/log/freeswitch-logs/
```

Expected output:
```
drwxr-xr-x 1 logger logger 4.0K Nov 11 12:00 .
drwxr-xr-x 1 root   root   4.0K Nov 11 11:59 ..
-rw-r--r-- 1 logger logger  1.2K Nov 11 12:00 default.log
-rw-r--r-- 1 logger logger  2.3K Nov 11 12:00 domain1.com.log
-rw-r--r-- 1 logger logger  1.8K Nov 11 12:00 domain2.com.log
```

### 4. Check Metrics
```bash
docker-compose logs freeswitch-logger | grep Metrics
```

Expected output:
```
Metrics: {"events_processed": 1234, "logs_written": 5000, "domains": 2, ...}
```

### 5. Verify Connection
```bash
docker-compose logs freeswitch-logger | head -20
```

Expected output:
```
2025-11-11 12:00:00,123 - freeswitch-logger - INFO - ============================================================
2025-11-11 12:00:00,124 - freeswitch-logger - INFO - FreeSWITCH Log Collector - Production Ready
2025-11-11 12:00:00,125 - freeswitch-logger - INFO - ============================================================
2025-11-11 12:00:00,126 - freeswitch-logger - INFO - ESL Host: your-freeswitch-server:8021
2025-11-11 12:00:00,127 - freeswitch-logger - INFO - Log Directory: /var/log/freeswitch-logs
2025-11-11 12:00:00,128 - freeswitch-logger - INFO - ✓ Successfully connected to FreeSWITCH
2025-11-11 12:00:00,129 - freeswitch-logger - INFO - ✓ Subscribed to all events
```

---

## Production Hardening

### 1. Resource Limits

Add to `docker-compose.yml`:
```yaml
services:
  freeswitch-logger:
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 512M
        reservations:
          cpus: '0.5'
          memory: 256M
```

### 2. Log Rotation (Host Level)

Create `/etc/logrotate.d/freeswitch-logger`:
```
/path/to/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    notifempty
    create 0644 logger logger
    sharedscripts
    postrotate
        docker-compose -f /path/to/docker-compose.yml exec -T freeswitch-logger kill -HUP 1
    endscript
}
```

### 3. Monitoring & Alerts

Set up alerts for:
- Container restart (unhealthy)
- Error count > threshold
- Memory usage > 80%
- Disk usage > 90%
- No events received for 60+ seconds

### 4. Backup Strategy

```bash
# Daily backup script
#!/bin/bash
BACKUP_DIR=/backups/freeswitch-logs
DATE=$(date +%Y%m%d_%H%M%S)
docker-compose exec -T freeswitch-logger \
  tar czf - /var/log/freeswitch-logs | \
  gzip > $BACKUP_DIR/logs_$DATE.tar.gz
```

### 5. Security

- Use strong ESL password
- Restrict network access to ESL port
- Run container with read-only root filesystem (if possible)
- Keep Docker images updated
- Use private registry for custom images

---

## Troubleshooting During Deployment

### Build Fails
```bash
# Check Docker disk space
docker system df

# Clean up
docker system prune -a

# Rebuild
docker-compose build --no-cache
```

### Container Won't Start
```bash
# Check logs
docker-compose logs freeswitch-logger

# Check environment
docker-compose config

# Test manually
docker run -it --rm freeswitch-logger:latest python3 /app/healthcheck.py
```

### Connection Failed
```bash
# Test network connectivity
docker-compose exec freeswitch-logger ping <ESL_HOST>

# Test ESL port
docker-compose exec freeswitch-logger nc -zv <ESL_HOST> <ESL_PORT>

# Check credentials
docker-compose exec freeswitch-logger python3 -c "import ESL; conn = ESL.ESLconnection(...)"
```

### No Logs Appearing
```bash
# Check permissions
docker-compose exec freeswitch-logger ls -la /var/log/freeswitch-logs/

# Check for errors
docker-compose logs freeswitch-logger | grep ERROR

# Monitor live
docker-compose exec freeswitch-logger tail -f /var/log/freeswitch-logs/default.log
```

---

## Post-Deployment

### 1. Monitor First 24 Hours
- Watch for any connection issues
- Verify all domains are captured
- Check metrics and performance
- Monitor disk usage growth

### 2. Optimize Configuration
- Adjust buffer flush interval if needed
- Fine-tune log rotation size
- Add custom domain patterns if needed
- Adjust MAX_FILE_DESCRIPTORS based on actual domain count

### 3. Set Up Monitoring
- Integrate with monitoring system (Prometheus, etc.)
- Set up alerts for critical issues
- Create dashboards for metrics
- Configure log aggregation if needed

### 4. Establish Procedures
- Log backup schedule
- Log retention policy
- Monitoring and alerting runbooks
- Disaster recovery plan

---

## Maintenance

### Regular Tasks
- Monitor disk usage
- Review logs for errors
- Update Docker images
- Check system resources
- Verify backups

### Scaling
- Add more log storage as needed
- Increase MAX_FILE_DESCRIPTORS for more domains
- Adjust performance parameters
- Consider log shipping for central management

---

## Support & Documentation

- README.md - Quick reference and troubleshooting
- SOLUTION_SUMMARY.md - Technical details
- docker-compose.yml - Service configuration
- .env.template - Configuration reference

---
