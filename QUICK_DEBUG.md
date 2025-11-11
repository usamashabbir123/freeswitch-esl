# Quick Reference: Debug ESL Connection Issues

## TL;DR - What to Do Now

1. **Test if FreeSWITCH is reachable:**
   ```bash
   telnet 192.168.1.157 8021
   ```
   - If connects → move to step 2
   - If fails → FreeSWITCH is down or unreachable

2. **Test if ESL is enabled:**
   ```bash
   # In telnet session, type:
   auth Expertflow123
   ```
   - If `+OK accept :-)` → ESL is working
   - If error → wrong password or ESL not enabled

3. **Run our debug script:**
   ```bash
   docker run --rm --env-file .env freeswitch-logger:latest python3 /app/debug_connection.py
   ```

4. **If debug passes, start the logger:**
   ```bash
   docker-compose up -d
   docker-compose logs -f
   ```

---

## Debugging Flowchart

```
Can you reach 192.168.1.157:8021?
│
├─ NO → Go to "FreeSWITCH Not Reachable" section
│
└─ YES → Is ESL enabled?
         │
         ├─ NO → Enable ESL in FreeSWITCH config
         │       (See TROUBLESHOOTING.md > Step 2)
         │
         └─ YES → Run debug_connection.py
                  │
                  ├─ PASSES → Start logger (docker-compose up)
                  │
                  └─ FAILS → Check error message
                             ├─ DNS error → Wrong hostname
                             ├─ Auth error → Wrong password
                             └─ Other → See TROUBLESHOOTING.md
```

---

## One-Liner Tests

### Test TCP connectivity
```bash
# Linux/Mac
timeout 5 bash -c 'cat < /dev/null > /dev/tcp/192.168.1.157/8021' && echo "✓ Connected" || echo "✗ Failed"

# Windows PowerShell
(New-Object System.Net.Sockets.TcpClient).Connect("192.168.1.157", 8021) -and "✓ Connected" -or "✗ Failed"

# Docker
docker run --rm alpine sh -c "nc -zv 192.168.1.157 8021 && echo '✓' || echo '✗'"
```

### Test ESL authentication
```bash
# Linux/Mac
(echo "auth Expertflow123" && sleep 1) | telnet 192.168.1.157 8021 | grep -q "accept" && echo "✓ Auth OK" || echo "✗ Auth Failed"

# Docker
docker run --rm --env-file .env freeswitch-logger:latest python3 /app/debug_connection.py
```

---

## Common Error Messages & Solutions

### "Connection refused" (errno: 111/11)
**Cause:** FreeSWITCH is not listening on that port
**Fix:**
```bash
# Check if FreeSWITCH is running
ps aux | grep freeswitch

# Check if port is open
netstat -tlnp | grep 8021

# If not listening, check ESL config:
cat /etc/freeswitch/autoload_configs/event_socket.conf.xml | grep -A2 "binding"
```

### "Connection timed out"
**Cause:** Firewall or network issue
**Fix:**
```bash
# Check firewall
sudo ufw status
sudo ufw allow 8021/tcp

# Or if using Windows Firewall:
netsh advfirewall firewall add rule name="ESL" dir=in action=allow protocol=tcp localport=8021
```

### "No route to host" (errno: 113)
**Cause:** IP address is unreachable or doesn't exist
**Fix:**
```bash
# Verify IP is correct
ping 192.168.1.157

# If ping fails, update .env with correct IP:
# ESL_HOST=correct.ip.address
```

### "DNS resolution failed"
**Cause:** Hostname (not IP) is not resolving
**Fix:**
```bash
# If using hostname instead of IP:
nslookup freeswitch.example.com

# If doesn't resolve, use IP instead:
ESL_HOST=192.168.1.157
```

### "ModuleNotFoundError: No module named 'imp'"
**Status:** ✅ FIXED - Already patched in this version

### "Failed to establish connection (not connected)"
**Cause:** TCP connected but ESL handshake failed
**Likely:** Wrong password or ESL module issue
**Fix:**
```bash
# Verify password in both places match:
# 1. FreeSWITCH: /etc/freeswitch/autoload_configs/event_socket.conf.xml
# 2. .env file: ESL_PASSWORD=Expertflow123
```

---

## Files Changed in This Session

1. **esl-python/freeswitchESL/ESL.py**
   - Fixed: Removed deprecated `imp` module usage
   - Changed: Use `importlib` for module loading

2. **.env**
   - Fixed: Removed inline comments (broke parsing)
   - Changed: Comments now on separate lines

3. **logger.py**
   - Enhanced: Added detailed connection debugging
   - Added: TCP reachability check before ESL connection
   - Added: Better error messages with troubleshooting hints

4. **debug_connection.py** (NEW)
   - Purpose: Comprehensive connection testing tool
   - Tests: TCP, DNS, ESL import, ESL connection, log directory

5. **Dockerfile**
   - Updated: Includes debug_connection.py in image

6. **TROUBLESHOOTING.md** (NEW)
   - Purpose: Comprehensive troubleshooting guide
   - Includes: 8 diagnostic steps + common solutions

7. **CONNECTION_DIAGNOSTICS.md** (NEW)
   - Purpose: Quick reference for current connection issue
   - Shows: What was fixed vs what needs to be checked

---

## When Everything Works

You'll see in Docker logs:
```
✓ Successfully connected to FreeSWITCH
✓ Subscribed to all events
Starting main collection loop...

[Metrics] Events: 1024 | Logs: 1024 | Domains: 3 | Bytes: 2.1MB | Memory: 45MB
[Metrics] Events: 2048 | Logs: 2048 | Domains: 5 | Bytes: 4.2MB | Memory: 48MB
```

And logs will be in:
```
/var/log/freeswitch-logs/
  ├── default.log
  ├── domain1.com.log
  ├── domain2.com.log
  └── domain3.com.log
```

---

## Still Stuck?

Check these in order:
1. Run: `docker run --rm --env-file .env freeswitch-logger:latest python3 /app/debug_connection.py`
2. Read the error message carefully
3. Refer to "Common Error Messages & Solutions" above
4. Check `TROUBLESHOOTING.md` for detailed steps
5. Verify: `cat .env | grep -E "ESL_HOST|ESL_PASSWORD"`
6. Verify: FreeSWITCH is running on the server at that IP

---

## Support Commands

```bash
# Check Docker image is built
docker images | grep freeswitch-logger

# Check .env is correct
cat .env

# Run debug
docker run --rm --env-file .env freeswitch-logger:latest python3 /app/debug_connection.py

# Check logs
docker-compose logs freeswitch-logger

# Check container is running
docker ps | grep freeswitch

# Get inside container
docker exec -it freeswitch-logger bash

# Check log files
docker exec freeswitch-logger ls -la /var/log/freeswitch-logs/
```
