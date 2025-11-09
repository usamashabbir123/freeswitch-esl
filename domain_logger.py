import os
import socket
from dotenv import load_dotenv
from datetime import datetime
import re

load_dotenv()

FS_HOST = os.getenv("FS_HOST", "127.0.0.1")
FS_PORT = int(os.getenv("FS_PORT", 8021))
LOG_DIR = os.getenv("LOG_DIR", "/var/logs/freeswitch")

os.makedirs(LOG_DIR, exist_ok=True)

def get_domain_logfile(domain):
    """Return path for the domain log file"""
    filename = f"{domain}.log"
    return os.path.join(LOG_DIR, filename)

def log_message(domain, message):
    """Append log message to the domain file"""
    path = get_domain_logfile(domain)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a") as f:
        f.write(f"[{timestamp}] {message}\n")

def connect_fs():
    """Connect to FreeSWITCH ESL port"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((FS_HOST, FS_PORT))
    return s

def parse_domain_from_event(event_line):
    """
    Parse domain from FreeSWITCH event line.
    Adjust this regex based on the event format from your FreeSWITCH server.
    """
    match = re.search(r"domain:([^\s]+)", event_line)
    if match:
        return match.group(1)
    return "default"

def main():
    print(f"Starting FreeSWITCH domain logger... connecting to {FS_HOST}:{FS_PORT}")
    try:
        s = connect_fs()
        log_message("default", "Connected to FreeSWITCH")

        buffer = b""
        while True:
            data = s.recv(4096)
            if not data:
                break
            buffer += data

            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.decode().strip()
                domain = parse_domain_from_event(line)
                log_message(domain, line)

    except Exception as e:
        log_message("default", f"Error: {str(e)}")
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
