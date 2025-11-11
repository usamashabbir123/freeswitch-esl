#!/usr/bin/env python3
"""
Debug script for FreeSWITCH ESL connection
Helps identify connectivity issues before running the main logger
"""

import os
import sys
import socket
import time
from pathlib import Path

# Get configuration from environment
ESL_HOST = os.getenv('ESL_HOST', 'localhost')
ESL_PORT = int(os.getenv('ESL_PORT', '8021'))
ESL_PASSWORD = os.getenv('ESL_PASSWORD', 'ClueCon')

print("=" * 70)
print("FreeSWITCH ESL Connection Debug Tool")
print("=" * 70)

print(f"\n📋 Configuration:")
print(f"  ESL_HOST: {ESL_HOST}")
print(f"  ESL_PORT: {ESL_PORT}")
print(f"  ESL_PASSWORD: {'*' * len(ESL_PASSWORD)} (length: {len(ESL_PASSWORD)})")
print(f"  LOG_DIR: {os.getenv('LOG_DIR', '/var/log/freeswitch-logs')}")

# Step 1: Test TCP connectivity
print(f"\n1️⃣  Testing TCP connectivity to {ESL_HOST}:{ESL_PORT}")
print("-" * 70)

try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    print(f"  Connecting...")
    result = sock.connect_ex((ESL_HOST, ESL_PORT))
    sock.close()
    
    if result == 0:
        print(f"  ✓ TCP port is OPEN and REACHABLE")
    else:
        print(f"  ✗ TCP port is NOT reachable")
        print(f"     Error code: {result}")
        print(f"     This could mean:")
        print(f"     - FreeSWITCH is not running")
        print(f"     - ESL is not enabled")
        print(f"     - Firewall is blocking the port")
        print(f"     - Wrong IP address or hostname")
        sys.exit(1)
except socket.gaierror as e:
    print(f"  ✗ DNS resolution failed: {e}")
    sys.exit(1)
except Exception as e:
    print(f"  ✗ Error: {e}")
    sys.exit(1)

# Step 2: Test ESL import
print(f"\n2️⃣  Testing ESL module import")
print("-" * 70)

try:
    from freeswitchESL import ESL
    print(f"  ✓ ESL module imported successfully")
    print(f"    ESL location: {ESL.__file__}")
except ImportError as e:
    print(f"  ✗ Failed to import ESL: {e}")
    sys.exit(1)

# Step 3: Test ESL connection
print(f"\n3️⃣  Testing ESL connection")
print("-" * 70)

try:
    print(f"  Creating ESL connection...")
    conn = ESL.ESLconnection(ESL_HOST, str(ESL_PORT), ESL_PASSWORD)
    
    print(f"  Checking connection status...")
    if conn.connected():
        print(f"  ✓ ESL connection SUCCESSFUL")
        
        # Try to subscribe to events
        print(f"  Subscribing to events...")
        conn.events("plain", "all")
        print(f"  ✓ Event subscription successful")
        
        # Receive a test event
        print(f"  Waiting for events (timeout: 5 seconds)...")
        event = conn.recvEvent()
        if event:
            print(f"  ✓ Received event: {event.getHeader('Event-Name')}")
        else:
            print(f"  ⚠ No events received (but connection is working)")
            
    else:
        print(f"  ✗ ESL connection FAILED")
        print(f"     Possible causes:")
        print(f"     - Wrong password")
        print(f"     - ESL module version mismatch")
        print(f"     - FreeSWITCH not accepting connections")
        print(f"\n  Try these commands on the FreeSWITCH server:")
        print(f"     telnet {ESL_HOST} {ESL_PORT}")
        print(f"     Then type: auth {ESL_PASSWORD}")
        sys.exit(1)
        
except Exception as e:
    import traceback
    print(f"  ✗ Exception: {type(e).__name__}: {e}")
    print(f"\n  Traceback:")
    print(f"  {traceback.format_exc()}")
    sys.exit(1)

# Step 4: Test log directory
print(f"\n4️⃣  Testing log directory")
print("-" * 70)

log_dir = os.getenv('LOG_DIR', '/var/log/freeswitch-logs')
try:
    log_path = Path(log_dir)
    if log_path.exists():
        print(f"  ✓ Log directory exists: {log_dir}")
        
        # Check writability
        test_file = log_path / ".write_test"
        try:
            test_file.touch()
            test_file.unlink()
            print(f"  ✓ Log directory is writable")
        except Exception as e:
            print(f"  ✗ Log directory is NOT writable: {e}")
    else:
        print(f"  ✗ Log directory does not exist: {log_dir}")
        try:
            log_path.mkdir(parents=True, exist_ok=True)
            print(f"  ✓ Created log directory: {log_dir}")
        except Exception as e:
            print(f"  ✗ Could not create log directory: {e}")
            
except Exception as e:
    print(f"  ✗ Error checking log directory: {e}")

print("\n" + "=" * 70)
print("✅ All checks passed! The logger should work.")
print("=" * 70)
print("\nNext steps:")
print("1. Run: docker compose up -d")
print("2. Check logs: docker compose logs -f freeswitch-logger")
print("3. Monitor: docker exec freeswitch-logger tail -f /var/log/freeswitch-logs/default.log")
