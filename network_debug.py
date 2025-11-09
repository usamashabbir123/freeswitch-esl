#!/usr/bin/env python3
"""
network_debug.py

Run a set of lightweight network and ESL connectivity checks from inside the container
and print a clear report you can paste back here.

This script uses only the Python standard library and shell fallbacks (if available)
so it works in minimal containers. It will:
 - print environment variables used by the logger
 - try several system commands (ip, ifconfig, route, hostname -I) if available
 - attempt DNS resolution of the ESL host
 - run ping (system ping) if available
 - attempt raw TCP connect to port (banner read)
 - attempt to speak the simple ESL auth handshake over TCP
 - attempt to use python-ESL binding (if installed) and report detailed errors

Run this inside the container where the logger runs and paste the full output here.
"""

import os
import sys
import socket
import subprocess
import time
from typing import Optional


def run_cmd(cmd):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        out = r.stdout.decode(errors='ignore')
        err = r.stderr.decode(errors='ignore')
        return (r.returncode, out.strip(), err.strip())
    except FileNotFoundError:
        return (127, '', f'command not found: {cmd[0]}')
    except subprocess.TimeoutExpired:
        return (124, '', 'timeout')
    except Exception as e:
        return (1, '', f'error running command: {e}')


def try_system_info():
    cmds = [
        (['ip', 'addr'], 'ip addr'),
        (['ip', 'route'], 'ip route'),
        (['ifconfig', '-a'], 'ifconfig -a'),
        (['route', '-n'], 'route -n'),
        (['hostname', '-I'], 'hostname -I'),
        (['cat', '/etc/resolv.conf'], 'resolv.conf'),
    ]
    results = {}
    for cmd, name in cmds:
        code, out, err = run_cmd(cmd)
        results[name] = {'rc': code, 'out': out, 'err': err}
    return results


def try_ping(host):
    ping_cmd = ['ping', '-c', '3', host]
    code, out, err = run_cmd(ping_cmd)
    return {'rc': code, 'out': out, 'err': err}


def resolve_host(host):
    try:
        addrs = socket.getaddrinfo(host, None)
        uniq = sorted({a[4][0] for a in addrs})
        return (True, uniq)
    except Exception as e:
        return (False, str(e))


def tcp_banner(host, port, timeout=5):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as s:
            s.settimeout(2)
            try:
                data = s.recv(1024)
            except Exception:
                data = b''
            reply = b''
            try:
                pw = os.getenv('ESL_PASSWORD', '')
                if pw:
                    s.sendall(f"auth {pw}\r\n\r\n".encode())
                    time.sleep(0.2)
                    s.settimeout(2)
                    try:
                        reply = s.recv(2048)
                    except Exception:
                        reply = b''
            except Exception:
                reply = b''
            return (True, data.decode(errors='ignore'), reply.decode(errors='ignore'))
    except Exception as e:
        return (False, '', repr(e))


def try_python_esl(host, port, pw):
    try:
        import ESL  # type: ignore
    except Exception as e:
        return {'available': False, 'error': f'Import failed: {e}'}

    try:
        con = ESL.ESLconnection(host, str(port), pw)
    except Exception as e:
        return {'available': True, 'connected': False, 'error': f'ESL.ESLconnection raised: {e}'}

    try:
        ok = bool(con and con.connected())
    except Exception as e:
        return {'available': True, 'connected': False, 'error': f'connected() check failed: {e}'}

    if not ok:
        return {'available': True, 'connected': False, 'error': 'connection object not connected'}

    try:
        con.events('plain', 'all')
        e = con.recvEvent()
        return {'available': True, 'connected': True, 'recvEvent_sample': str(bool(e))}
    except Exception as e:
        return {'available': True, 'connected': True, 'error': f'recvEvent failed: {e}'}


def main():
    host = os.getenv('ESL_HOST', '192.168.1.157')
    port = os.getenv('ESL_PORT', '8021')
    pw = os.getenv('ESL_PASSWORD', 'ClueCon')

    print('=== NETWORK DEBUG REPORT ===')
    print(f'ESL_HOST={host} ESL_PORT={port} ESL_PASSWORD={("*"*len(pw)) if pw else "(empty)"}')
    print('\n-- System commands --')
    sysinfo = try_system_info()
    for k, v in sysinfo.items():
        print(f'\n# {k} (rc={v["rc"]})')
        if v['out']:
            print(v['out'])
        if v['err']:
            print('\n[stderr]')
            print(v['err'])

    print('\n-- DNS resolution --')
    ok, res = resolve_host(host)
    if ok:
        print('Resolved addresses:', ', '.join(res))
    else:
        print('Resolve error:', res)

    print('\n-- system ping (if available) --')
    ping_res = try_ping(host)
    print(f'ping rc={ping_res["rc"]}')
    if ping_res['out']:
        print(ping_res['out'])
    if ping_res['err']:
        print('\n[ping stderr]')
        print(ping_res['err'])

    print('\n-- raw TCP connect + ESL auth attempt --')
    success, banner, authreply = tcp_banner(host, port, timeout=5)
    print('tcp_connect_success=', success)
    if banner:
        print('\n[banner]\n' + banner)
    if authreply:
        print('\n[auth reply]\n' + authreply)

    print('\n-- python-ESL binding test (if installed) --')
    esl_res = try_python_esl(host, port, pw)
    print(esl_res)

    print('\n=== END REPORT ===')


if __name__ == '__main__':
    main()
