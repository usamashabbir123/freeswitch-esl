# FreeSWITCH ESL Connection Troubleshooting Guide

## Issue
Your container cannot connect to FreeSWITCH at `192.168.1.157:8021`

Error: `TCP port is NOT reachable (errno: 11)`

## Troubleshooting Steps

### Step 1: Verify FreeSWITCH is Running

**On the FreeSWITCH server (192.168.1.157):**

```bash
# Check if FreeSWITCH process is running
ps aux | grep freeswitch

# Or using systemctl
systemctl status freeswitch

# Check if port 8021 is listening
netstat -tlnp | grep 8021
# or
ss -tlnp | grep 8021
# or (Windows)
netstat -ano | findstr :8021
```

**Expected output:** Should show FreeSWITCH listening on 0.0.0.0:8021 or 127.0.0.1:8021

### Step 2: Check ESL is Enabled in FreeSWITCH

FreeSWITCH ESL module might not be enabled. Check the configuration:

**Location:** `/etc/freeswitch/autoload_configs/event_socket.conf.xml` (Linux/Docker)

Look for a line like:
```xml
<binding name="socket 1 192.168.1.157:8021">
  <param name="password" value="ClueCon"/>
</binding>
```

Or it might be under:
**Location:** `/usr/local/freeswitch/conf/autoload_configs/event_socket.conf.xml`

### Step 3: Test Connectivity Manually

**From the Docker host or any machine with network access to 192.168.1.157:**

```bash
# Test TCP connection
telnet 192.168.1.157 8021

# Or using nc (netcat)
nc -zv 192.168.1.157 8021

# Or from Docker container
docker run --rm alpine sh -c "nc -zv 192.168.1.157 8021"

# Or using Python
docker run --rm python:3.12 python3 -c "
import socket
sock = socket.socket()
sock.connect(('192.168.1.157', 8021))
print('Connected!')
sock.close()
"
```

**Expected output:** Should say "Connected" or show the port is open

### Step 4: Verify Firewall Rules

**On FreeSWITCH server (Linux):**

```bash
# Check if firewall is blocking
sudo ufw status
sudo ufw allow 8021/tcp

# On Windows
netsh advfirewall firewall add rule name="FreeSWITCH ESL" dir=in action=allow protocol=tcp localport=8021
```

### Step 5: Verify Credentials

If connection works but authentication fails, check:

**In FreeSWITCH config (`event_socket.conf.xml`):**
```xml
<param name="password" value="YOUR_PASSWORD"/>
```

**In your `.env` file:**
```
ESL_PASSWORD=YOUR_PASSWORD
```

They must match exactly!

### Step 6: Check ESL Binding Address

The ESL module might be bound to a specific interface. Common configurations:

```xml
<!-- Binds to all interfaces (most permissive) -->
<binding name="socket 1 0.0.0.0:8021">
</binding>

<!-- Binds to localhost only (won't work for remote connections) -->
<binding name="socket 1 127.0.0.1:8021">
</binding>

<!-- Binds to specific interface -->
<binding name="socket 1 192.168.1.157:8021">
</binding>
```

### Step 7: Check FreeSWITCH Logs

**On the FreeSWITCH server:**

```bash
# View logs (location varies by OS)
tail -f /var/log/freeswitch/freeswitch.log
# or
tail -f /usr/local/freeswitch/log/freeswitch.log
# or in Docker
docker logs freeswitch -f

# Search for ESL errors
grep -i "event_socket\|ESL" freeswitch.log
```

### Step 8: Is FreeSWITCH in a Docker Container?

If FreeSWITCH is also running in Docker, the configuration is different:

**Option A: Both containers on same network**
```yaml
version: '3'
services:
  freeswitch:
    image: freeswitch:latest
    networks:
      - app_network

  freeswitch-logger:
    image: freeswitch-logger:latest
    environment:
      ESL_HOST: freeswitch  # Use service name, not IP
      ESL_PORT: 8021
      ESL_PASSWORD: Expertflow123
    depends_on:
      - freeswitch
    networks:
      - app_network

networks:
  app_network:
    driver: bridge
```

**Option B: Container needs to access host**

On Linux/Mac (Docker Desktop):
```bash
# Use host.docker.internal (Docker Desktop)
ESL_HOST=host.docker.internal
```

On Linux (Docker Engine):
```bash
# Use the host IP
ESL_HOST=192.168.1.157
```

### Quick Diagnostic Commands

Run these from your Docker container:

```bash
# Check if you can reach the server at all
docker exec freeswitch-logger ping -c 3 192.168.1.157

# Try telnet
docker exec freeswitch-logger sh -c "apt-get update && apt-get install -y telnet && telnet 192.168.1.157 8021"

# Try with our debug script
docker exec freeswitch-logger python3 /app/debug_connection.py

# Check DNS resolution
docker exec freeswitch-logger nslookup 192.168.1.157
```

## Common Solutions

### Solution 1: FreeSWITCH Not Running
```bash
# Restart FreeSWITCH
service freeswitch restart
# or
systemctl restart freeswitch
```

### Solution 2: ESL Module Not Loaded
Check `/etc/freeswitch/autoload_configs/event_socket.conf.xml` exists and is not commented out.

Then reload:
```bash
freeswitch -c  # Or use fs_cli
fs_cli> reloadxml
fs_cli> shutdown
```

### Solution 3: Firewall Blocking
```bash
# Linux
sudo ufw allow 8021/tcp

# Or disable firewall temporarily for testing
sudo ufw disable
```

### Solution 4: Wrong IP/Port
Update your `.env`:
```bash
# Get the actual FreeSWITCH IP
ping freeswitch.example.com

# Then update
ESL_HOST=correct.ip.address
ESL_PORT=8021
```

## Next Steps After Fixing

Once the debug script shows ✅ success:

1. Rebuild the Docker image (cached):
```bash
docker build -t freeswitch-logger:latest .
```

2. Run with docker-compose:
```bash
docker-compose down
docker-compose up -d
```

3. Check logs:
```bash
docker-compose logs -f freeswitch-logger
```

4. Verify logs are being created:
```bash
docker exec freeswitch-logger ls -la /var/log/freeswitch-logs/
docker exec freeswitch-logger tail -f /var/log/freeswitch-logs/default.log
```

## Still Having Issues?

Provide this information:

1. **What does `telnet 192.168.1.157 8021` show?**
2. **FreeSWITCH OS:** (Linux, Windows, Docker, etc.)
3. **Output of:** `netstat -tlnp | grep 8021` (or equivalent on your OS)
4. **Content of:** `/etc/freeswitch/autoload_configs/event_socket.conf.xml` (or equivalent)
5. **Latest FreeSWITCH logs** (10-20 lines showing startup)
