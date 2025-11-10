#!/usr/bin/env python3

import os
import sys
import ESL

try:
    fs_host = os.getenv("FS_HOST", "127.0.0.1")
    fs_port = int(os.getenv("FS_PORT", "8021"))
    fs_password = os.getenv("FS_PASSWORD", "ClueCon")

    conn = ESL.ESLconnection(fs_host, fs_port, fs_password)
    
    if conn.connected():
        print("Health check OK: Successfully connected to FreeSWITCH ESL.")
        conn.disconnect()
        sys.exit(0)
    else:
        print("Health check FAILED: Could not connect to FreeSWITCH ESL.")
        sys.exit(1)

except Exception as e:
    print(f"Health check FAILED: {str(e)}")
    sys.exit(1)