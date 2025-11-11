#!/usr/bin/env python3
"""
Health check script for FreeSWITCH Log Collector
Verifies connectivity and log file creation
"""

import os
import sys
import json
from pathlib import Path

try:
    # Import ESL
    try:
        from freeswitchESL import ESL
    except ImportError:
        print("UNHEALTHY: ESL module not available")
        sys.exit(1)
    
    # Get configuration
    esl_host = os.getenv("ESL_HOST", "localhost")
    esl_port = int(os.getenv("ESL_PORT", "8021"))
    esl_password = os.getenv("ESL_PASSWORD", "ClueCon")
    log_dir = os.getenv("LOG_DIR", "/var/log/freeswitch-logs")
    
    health_status = {
        "status": "unknown",
        "checks": {}
    }
    
    # Check 1: Verify log directory exists and is writable
    try:
        log_path = Path(log_dir)
        if not log_path.exists():
            health_status["checks"]["log_dir_exists"] = False
            print(f"⚠ Warning: Log directory does not exist: {log_dir}")
        else:
            # Try to create a test file
            test_file = log_path / ".healthcheck"
            test_file.touch()
            test_file.unlink()
            health_status["checks"]["log_dir_writable"] = True
    except Exception as e:
        health_status["checks"]["log_dir_writable"] = False
        print(f"✗ Error: Log directory not writable: {e}")
    
    # Check 2: ESL connectivity
    try:
        conn = ESL.ESLconnection(esl_host, str(esl_port), esl_password)
        
        if conn.connected():
            health_status["checks"]["esl_connected"] = True
            
            # Try to get system info
            try:
                info = conn.api("status")
                if info and info.getBody():
                    health_status["checks"]["esl_responsive"] = True
                conn.disconnect()
            except:
                conn.disconnect()
                health_status["checks"]["esl_responsive"] = False
        else:
            health_status["checks"]["esl_connected"] = False
            print(f"✗ Error: Could not connect to FreeSWITCH at {esl_host}:{esl_port}")
    except Exception as e:
        health_status["checks"]["esl_connected"] = False
        print(f"✗ Error: ESL connection failed: {e}")
    
    # Determine overall health
    all_checks = health_status["checks"].values()
    if all(all_checks):
        health_status["status"] = "healthy"
        print("✓ Health check OK: All systems operational")
        sys.exit(0)
    elif any(all_checks):
        health_status["status"] = "degraded"
        print("⚠ Health check PARTIAL: Some systems operational")
        print(json.dumps(health_status, indent=2))
        sys.exit(0)  # Exit 0 for partial health to allow startup
    else:
        health_status["status"] = "unhealthy"
        print("✗ Health check FAILED: No systems operational")
        print(json.dumps(health_status, indent=2))
        sys.exit(1)

except Exception as e:
    print(f"✗ Health check FAILED: {str(e)}")
    sys.exit(1)
