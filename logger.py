#!/usr/bin/env python3
"""
FreeSWITCH ESL Log Collector - Production Ready

This is a corrected, production-ready version of the collector you provided.
Key improvements:
  - Robust file-opening with fallback directory and explicit utf-8 encoding
  - Metrics for writes (bytes/logs)
  - Safe rotation and immediate re-opening after rotation
  - Optional fsync-on-write controlled via SYNC_ON_WRITE env var
  - Improved oldest-handle eviction by last-access time
  - Proper signal handling that calls collector.shutdown()
  - Application log file handler (rotating file) optional via APP_LOG_FILE
  - Defensive event handling and more helpful debug logging

Note: This code still depends on your freeswitch ESL Python bindings. Keep
`from freeswitchESL import ESL` if that matches your environment. If the
bindings expose slightly different method names, adjust the connect/recv
calls accordingly.
"""

import os
import sys
import time
import signal
import logging
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict, deque
from threading import Lock, Thread
import re
import json
import errno

try:
    from freeswitchESL import ESL
except Exception:
    print("ERROR: ESL module not found. Please ensure it's built and installed.")
    print("      Tried importing: from freeswitchESL import ESL")
    sys.exit(1)

import psutil

# Configuration from environment
ESL_HOST = os.getenv('ESL_HOST', 'localhost')
ESL_PORT = int(os.getenv('ESL_PORT', '8021'))
ESL_PASSWORD = os.getenv('ESL_PASSWORD', 'ClueCon')
LOG_DIR = os.getenv('LOG_DIR', '/var/log/freeswitch')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
RECONNECT_DELAY = int(os.getenv('RECONNECT_DELAY', '5'))
FILE_ROTATION_SIZE = int(os.getenv('FILE_ROTATION_SIZE', '104857600'))  # 100MB
BUFFER_FLUSH_INTERVAL = int(os.getenv('BUFFER_FLUSH_INTERVAL', '5'))  # seconds
MAX_FILE_DESCRIPTORS = int(os.getenv('MAX_FILE_DESCRIPTORS', '50'))
SYNC_ON_WRITE = os.getenv('SYNC_ON_WRITE', 'false').lower() in ('1', 'true', 'yes')
APP_LOG_FILE = os.getenv('APP_LOG_FILE', '')  # e.g. /var/log/freeswitch/collector.log

# Setup application logging (stdout + optional file rotating handler)
root_logger = logging.getLogger()
root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(formatter)
root_logger.handlers = [stdout_handler]

if APP_LOG_FILE:
    try:
        from logging.handlers import RotatingFileHandler
        Path(APP_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(APP_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
        fh.setFormatter(formatter)
        root_logger.addHandler(fh)
    except Exception as e:
        root_logger.warning(f"Failed to create app log file {APP_LOG_FILE}: {e}")

logger = logging.getLogger('freeswitch-logger')


class MetricsCollector:
    """Collects application metrics for monitoring"""

    def __init__(self):
        self.lock = Lock()
        self.events_processed = 0
        self.logs_written = 0
        self.domains_count = 0
        self.bytes_written = 0
        self.errors_count = 0
        self.last_event_time = time.time()

    def record_event(self):
        with self.lock:
            self.events_processed += 1
            self.last_event_time = time.time()

    def record_write(self, size):
        with self.lock:
            self.logs_written += 1
            self.bytes_written += size

    def record_domain(self, count):
        with self.lock:
            self.domains_count = count

    def record_error(self):
        with self.lock:
            self.errors_count += 1

    def get_metrics(self):
        with self.lock:
            return {
                'events_processed': self.events_processed,
                'logs_written': self.logs_written,
                'domains': self.domains_count,
                'bytes_written': self.bytes_written,
                'errors': self.errors_count,
                'time_since_last_event': time.time() - self.last_event_time,
                'memory_mb': psutil.Process().memory_info().rss / 1024 / 1024
            }


class LogManager:
    """Manages log files for different domains with buffering and rotation"""

    def __init__(self, log_dir, rotation_size, metrics):
        self.log_dir = Path(log_dir)
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            fallback = Path('/tmp/freeswitch-logs')
            try:
                fallback.mkdir(parents=True, exist_ok=True)
                logger.warning(f"Permission denied creating {self.log_dir}; falling back to {fallback}")
                self.log_dir = fallback
            except Exception as e:
                logger.error(f"Failed to create fallback log directory {fallback}: {e}")
                raise

        self.rotation_size = rotation_size
        self.file_handles = {}  # domain -> file object
        self.file_sizes = defaultdict(int)
        self.file_access = {}  # domain -> last access timestamp used to close oldest
        self.buffers = defaultdict(list)
        self.lock = Lock()
        self.running = True
        self.metrics = metrics

    def extract_domain(self, event, log_line):
        """
        Improved domain extraction: prefer session/ESL headers (including variable_ prefixes),
        then common SIP headers, then regex against the raw log line. Returns 'unknown' when
        nothing reliable is found.
        """
        if not event:
            return 'unknown'

        try:
            # Common header keys to try (include variable_ prefixed session variables)
            header_candidates = [
                'domain_name', 'Caller-Domain', 'Callee-Domain', 'User-Domain', 'Domain',
                'variable_domain_name', 'variable_domain', 'variable_user_domain',
                'variable_sip_from', 'variable_sip_to', 'variable_domain'
            ]

            for header in header_candidates:
                try:
                    val = event.getHeader(header)
                except Exception:
                    val = None
                if not val or not isinstance(val, str):
                    continue
                v = val.strip()
                if not v or v.lower() == 'default':
                    continue

                # If value looks like a SIP URI, extract domain
                if 'sip:' in v or '@' in v:
                    sip_domain = self._extract_sip_domain(v) or (v.split('@', 1)[1].strip() if '@' in v else None)
                    if sip_domain and self._is_valid_domain(sip_domain):
                        # Sanitize before returning (lowercase, remove invalid filename chars)
                        sip_domain = sip_domain.lower().strip()
                        sip_domain = re.sub(r'[<>:"/\\|?*\s]', '', sip_domain)
                        logger.debug(f"Domain from header {header}: {sip_domain}")
                        return sip_domain or 'unknown'

                # Plain domain value
                if self._is_valid_domain(v):
                    v = v.lower().strip()
                    v = re.sub(r'[<>:"/\\|?*\s]', '', v)
                    logger.debug(f"Domain from header {header}: {v}")
                    return v or 'unknown'

            # Try Caller-ID-Number (user@domain)
            try:
                caller_id = event.getHeader('Caller-ID-Number')
            except Exception:
                caller_id = None
            if caller_id and isinstance(caller_id, str) and '@' in caller_id:
                domain = caller_id.split('@', 1)[1].strip()
                if self._is_valid_domain(domain):
                    domain = domain.lower()
                    domain = re.sub(r'[<>:"/\\|?*\s]', '', domain)
                    logger.debug(f"Domain from Caller-ID-Number: {domain}")
                    return domain or 'unknown'

            # Try From/To/Channel-Name headers
            for hdr in ('From', 'To', 'Channel-Name'):
                try:
                    h = event.getHeader(hdr)
                except Exception:
                    h = None
                if not h:
                    continue
                dm = self._extract_sip_domain(h) or (str(h).split('@', 1)[1].strip() if '@' in str(h) else None)
                if dm and self._is_valid_domain(dm):
                    dm = dm.lower()
                    dm = re.sub(r'[<>:"/\\|?*\s]', '', dm)
                    logger.debug(f"Domain from {hdr}: {dm}")
                    return dm or 'unknown'

            # Final fallback: regexes against raw log line to catch fs_cli outputs
            if log_line and isinstance(log_line, str):
                patterns = [
                    r'sip:[^@>]+@([\w.-]+)',
                    r'sofia/[\w-]+/(?:[\w%+\-]+@)?([\w.-]+)',
                    r'@([\w.-]+)',
                    r'\[([\w.-]+\.[a-zA-Z]{2,})\]'
                ]
                for p in patterns:
                    try:
                        m = re.search(p, log_line)
                        if m:
                            d = m.group(1)
                            if d and self._is_valid_domain(d):
                                d = d.lower()
                                d = re.sub(r'[<>:"/\\|?*\s]', '', d)
                                logger.debug(f"Domain from log regex ({p}): {d}")
                                return d or 'unknown'
                    except Exception:
                        continue

            logger.debug("No domain found in headers or log; using 'unknown'")
            return 'unknown'

        except Exception as e:
            logger.exception(f"Error extracting domain: {e}")
            return 'unknown'

    @staticmethod
    def _extract_sip_domain(sip_string):
        try:
            match = re.search(r'sip:[\w.+-]*@([\w.-]+)', str(sip_string))
            if match:
                return match.group(1)
        except Exception:
            pass
        return None

    @staticmethod
    def _is_valid_domain(domain):
        if not domain:
            return False
        domain = domain.strip().lower()
        if domain == 'localhost':
            return True
        if len(domain) > 253:
            return False
        labels = domain.split('.')
        if len(labels) == 1:
            # allow simple hostnames (alnum, - , _)
            return all(c.isalnum() or c in '-_' for c in domain)
        for label in labels:
            if not label or len(label) > 63:
                return False
            if not all(c.isalnum() or c in '-_' for c in label):
                return False
        return True

    def write_log(self, domain, log_line):
        """Write log line to appropriate domain file - immediate write"""
        try:
            with self.lock:
                # Normalize domain: lowercase, strip, sanitize for filename
                domain = str(domain).lower().strip() if domain else 'unknown'
                # Remove only truly invalid filename characters: < > : " / \ | ? *
                # Keep dots, hyphens, underscores (valid in filenames and domain/IP names)
                domain = re.sub(r'[<>:"/\\|?*\s]', '', domain)
                domain = domain or 'unknown'
                
                # use timezone-aware UTC timestamps (avoid deprecated utcnow())
                timestamp = datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                # ensure single newline at end
                line = log_line.rstrip('\n')
                formatted_line = f"[{timestamp}] {line}\n"
                # Use internal writer
                self._write_to_file(domain, formatted_line)
        except Exception as e:
            logger.exception(f"Error writing log for domain={domain}: {e}")
            self.metrics.record_error()

    def flush_buffers(self):
        try:
            with self.lock:
                flushed_entries = 0
                for domain, entries in list(self.buffers.items()):
                    if not entries:
                        continue
                    content = ''.join(entries)
                    self._write_to_file(domain, content)
                    flushed_entries += len(entries)
                    entries.clear()
                if flushed_entries:
                    logger.debug(f"Flushed {flushed_entries} buffered log entries to disk")
                self.metrics.record_domain(len(self.file_handles))
        except Exception as e:
            logger.exception(f"Error flushing buffers: {e}")
            self.metrics.record_error()

    def _write_to_file(self, domain, content):
        # Normalize domain: lowercase, strip, sanitize for filename
        domain = str(domain).lower().strip() if domain else 'unknown'
        # Remove only truly invalid filename characters: < > : " / \ | ? *
        # Keep dots, hyphens, underscores (valid in filenames and domain/IP names)
        domain = re.sub(r'[<>:"/\\|?*\s]', '', domain)
        domain = domain or 'unknown'
        
        log_file = self.log_dir / f"{domain}.log"
        try:
            # Rotate if needed
            if log_file.exists():
                try:
                    current_size = log_file.stat().st_size
                except OSError:
                    current_size = self.file_sizes.get(domain, 0)
                if current_size + len(content.encode('utf-8')) >= self.rotation_size:
                    self._rotate_log(domain, log_file)

            # Enforce file descriptor limit
            if len(self.file_handles) >= MAX_FILE_DESCRIPTORS and domain not in self.file_handles:
                self._close_oldest_file()

            # Open file if necessary
            if domain not in self.file_handles or self.file_handles[domain].closed:
                # Ensure parent exists
                try:
                    self.log_dir.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
                f = open(log_file, 'a', encoding='utf-8', buffering=8192)
                self.file_handles[domain] = f
                # initialize size if unknown
                try:
                    self.file_sizes[domain] = log_file.stat().st_size
                except Exception:
                    self.file_sizes[domain] = 0

            f = self.file_handles[domain]
            f.write(content)
            f.flush()
            if SYNC_ON_WRITE:
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass

            bytes_written = len(content.encode('utf-8'))
            self.file_sizes[domain] = self.file_sizes.get(domain, 0) + bytes_written
            self.file_access[domain] = time.time()
            self.metrics.record_write(bytes_written)
            logger.debug(f"Wrote {bytes_written} bytes to {log_file} (total: {self.file_sizes[domain]})")

        except Exception as e:
            logger.exception(f"Error writing to {log_file}: {e}")
            self.metrics.record_error()
            # Clean up a broken handle
            if domain in self.file_handles:
                try:
                    self.file_handles[domain].close()
                except Exception:
                    pass
                del self.file_handles[domain]

    def _rotate_log(self, domain, log_file):
        try:
            if domain in self.file_handles and not self.file_handles[domain].closed:
                try:
                    self.file_handles[domain].close()
                except Exception:
                    pass
                del self.file_handles[domain]

            # rotation timestamp - timezone-aware
            timestamp = datetime.now(timezone.utc).astimezone().strftime('%Y%m%d_%H%M%S')
            rotated_file = self.log_dir / f"{domain}_{timestamp}.log"
            try:
                log_file.rename(rotated_file)
                logger.info(f"Rotated log file: {log_file} -> {rotated_file}")
            except Exception as e:
                logger.warning(f"Failed to rotate file {log_file}: {e}")

            self.file_sizes[domain] = 0
        except Exception as e:
            logger.exception(f"Error rotating log file {log_file}: {e}")
            self.metrics.record_error()

    def _close_oldest_file(self):
        try:
            if not self.file_handles:
                return
            oldest_domain = min(self.file_access.keys(), key=lambda d: self.file_access.get(d, 0))
            handle = self.file_handles.get(oldest_domain)
            if handle and not handle.closed:
                try:
                    handle.close()
                except Exception:
                    pass
            self.file_handles.pop(oldest_domain, None)
            self.file_access.pop(oldest_domain, None)
            logger.debug(f"Closed oldest file handle for domain: {oldest_domain}")
        except Exception as e:
            logger.exception(f"Error closing oldest file: {e}")

    def close_all(self):
        self.running = False
        self.flush_buffers()
        with self.lock:
            for domain, handle in list(self.file_handles.items()):
                try:
                    if handle and not handle.closed:
                        handle.close()
                except Exception as e:
                    logger.exception(f"Error closing handle for {domain}: {e}")
            self.file_handles.clear()
            self.file_access.clear()
        logger.info("All log files closed")


class FreeSwitchLogCollector:
    """Main collector class that connects to FreeSWITCH via ESL"""

    def __init__(self):
        self.metrics = MetricsCollector()
        self.connection = None
        self.log_manager = LogManager(LOG_DIR, FILE_ROTATION_SIZE, self.metrics)
        self.running = False
        self.last_flush = time.time()
        self.connection_attempts = 0
        self.max_connection_attempts = 5
        self.recent_event_ids = deque(maxlen=10000)

    def connect(self):
        try:
            logger.info(f"Attempting connection to FreeSWITCH at {ESL_HOST}:{ESL_PORT}")
            logger.info(f"Attempt {self.connection_attempts + 1}/{self.max_connection_attempts}")

            import socket
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                result = sock.connect_ex((ESL_HOST, ESL_PORT))
                sock.close()
                if result != 0:
                    logger.error(f"TCP port {ESL_PORT} on {ESL_HOST} is not reachable (errno: {result})")
                    self.connection_attempts += 1
                    return False
            except Exception as e:
                logger.warning(f"Connectivity check exception: {e}")

            self.connection = ESL.ESLconnection(ESL_HOST, str(ESL_PORT), ESL_PASSWORD)
            # Some bindings call connected(), some call is_connected(); try both
            connected = False
            try:
                connected = self.connection.connected()
            except Exception:
                try:
                    connected = getattr(self.connection, 'is_connected', lambda: False)()
                except Exception:
                    connected = False

            if connected:
                logger.info("Connected to FreeSWITCH")
    
                # Use raw log subscription to capture fs_cli-style output (file:line, severity)
                try:
                    self.connection.events("log", "all")
                    logger.info("✓ Subscribed to all RAW log events (log/all)")
                except Exception:
                    # Fallback to plain events if 'log' isn't supported
                    try:
                        self.connection.events("plain", "all")
                        logger.info("✓ Subscribed to parsed events (plain/all) - raw logs unavailable")
                    except Exception:
                        logger.warning("Failed to subscribe to events via ESL API")
                self.connection_attempts = 0
                return True
            else:
                logger.error('Failed to establish ESL connection')
                self.connection_attempts += 1
                return False
        except Exception as e:
            logger.exception(f"Connection error: {e}")
            self.connection_attempts += 1
            return False

    def process_event(self, event):
        try:
            event_id = None
            try:
                en = event.getHeader('Event-Name') or ''
                uid = event.getHeader('Unique-ID') or event.getHeader('Event-UUID') or ''
                event_id = f"{en}|{uid}"
            except Exception:
                event_id = None

            if event_id and event_id in self.recent_event_ids:
                logger.debug(f"Duplicate event ignored: {event_id}")
                return
            if event_id:
                self.recent_event_ids.append(event_id)

            self.metrics.record_event()
            event_name = event.getHeader('Event-Name') or ''

            log_data = None
            if event_name.upper() in ('LOG', 'CHANNEL_LOG'):
                try:
                    body = event.getBody() or ''
                except Exception:
                    body = ''

                # Prefer raw LOG format with severity and file:line when available
                log_level = None
                try:
                    log_level = event.getHeader('Log-Level') or event.getHeader('Severity')
                except Exception:
                    log_level = None
                if not log_level:
                    log_level = 'INFO'

                file_info = ''
                try:
                    f = event.getHeader('File') or ''
                    l = event.getHeader('Line') or ''
                    if f or l:
                        file_info = f"[{f}:{l}] " if f and l else f"[{f or l}] "
                except Exception:
                    file_info = ''

                formatted = f"[{log_level}] {file_info}{body}".strip()
                log_data = formatted

                # Also emit raw log to stdout so container logs mirror fs_cli
                try:
                    logger.info(formatted)
                except Exception:
                    logger.debug(formatted)
            elif event_name and event_name.upper() != 'HEARTBEAT':
                # Build a rich log line containing headers and body so files contain full call info
                priority = event.getHeader('Log-Level') or event.getHeader('Severity') or 'INFO'
                try:
                    body = event.getBody() or ''
                except Exception:
                    body = ''

                headers_of_interest = [
                    'Unique-ID', 'Channel-Name', 'Caller-ID-Number', 'Caller-ID-Name',
                    'Destination-Number', 'Caller-Domain', 'Callee-Domain', 'Channel-State',
                    'Call-Direction', 'Application', 'Application-Data'
                ]
                header_parts = []
                for h in headers_of_interest:
                    try:
                        val = event.getHeader(h)
                    except Exception:
                        val = None
                    if val:
                        header_parts.append(f"{h}={val}")

                header_str = ' '.join(header_parts)
                # include any body payload after headers
                combined = f"{header_str} {body}".strip()
                log_data = f"[{event_name}] [{priority}] {combined}".strip()
                # Also log the rich line to stdout for visibility
                try:
                    logger.info(log_data)
                except Exception:
                    logger.debug(log_data)

            if log_data:
                domain = self.log_manager.extract_domain(event, log_data)
                # Primary: write to domain-specific log
                self.log_manager.write_log(domain, log_data)
                # Also persist a full fs_cli-style stream for complete raw logs
                try:
                    self.log_manager.write_log('fs_cli', log_data)
                except Exception:
                    pass
        except Exception as e:
            logger.exception(f"Error processing event: {e}")
            self.metrics.record_error()

    def _metrics_worker(self):
        # background thread to emit metrics periodically
        while self.running:
            try:
                time.sleep(30)
                m = self.metrics.get_metrics()
                logger.info(f"Metrics: {json.dumps(m)}")
            except Exception:
                pass

    def run(self):
        self.running = True
        logger.info('Starting main collection loop...')

        metrics_thread = Thread(target=self._metrics_worker, daemon=True)
        metrics_thread.start()

        while self.running:
            try:
                if not self.connection or not getattr(self.connection, 'connected', lambda: False)():
                    if self.connection_attempts >= self.max_connection_attempts:
                        logger.error(f"Failed to connect after {self.max_connection_attempts} attempts; sleeping 30s")
                        time.sleep(30)
                        self.connection_attempts = 0
                        continue

                    logger.warning('Connection lost or not established, attempting to reconnect...')
                    if not self.connect():
                        logger.error(f"Reconnection failed, retrying in {RECONNECT_DELAY}s")
                        time.sleep(RECONNECT_DELAY)
                        continue

                # recvEventTimed may raise or return None
                try:
                    event = self.connection.recvEventTimed(1000)
                    if event:
                        self.process_event(event)
                except Exception as e:
                    logger.debug(f"recvEventTimed/recv error: {e}")

                # periodic flush
                current_time = time.time()
                if current_time - self.last_flush >= BUFFER_FLUSH_INTERVAL:
                    self.log_manager.flush_buffers()
                    self.last_flush = current_time

            except KeyboardInterrupt:
                logger.info('KeyboardInterrupt received')
                break
            except Exception as e:
                logger.exception(f"Error in main loop: {e}")
                self.metrics.record_error()
                time.sleep(1)

        self.shutdown()

    def shutdown(self):
        logger.info('Shutting down collector...')
        self.running = False
        try:
            if self.connection:
                try:
                    if getattr(self.connection, 'connected', lambda: False)():
                        self.connection.disconnect()
                        logger.info('Disconnected from FreeSWITCH')
                except Exception:
                    pass
        except Exception:
            pass

        self.log_manager.close_all()
        metrics = self.metrics.get_metrics()
        logger.info(f"Final metrics: {json.dumps(metrics)}")
        logger.info('Shutdown complete')

    def get_metrics(self):
        return self.metrics.get_metrics()


# Global collector reference for signal handling
COLLECTOR = None


def _signal_handler(signum, frame):
    logger.info(f"Received signal {signum}; initiating shutdown")
    try:
        if COLLECTOR:
            COLLECTOR.shutdown()
    except Exception:
        pass
    # wait a short moment then exit
    time.sleep(0.3)
    sys.exit(0)


def main():
    global COLLECTOR
    logger.info('=' * 60)
    logger.info('FreeSWITCH Log Collector - Production Ready')
    logger.info('=' * 60)
    logger.info(f"ESL Host: {ESL_HOST}:{ESL_PORT}")
    logger.info(f"Log Directory: {LOG_DIR}")
    logger.info(f"File Rotation Size: {FILE_ROTATION_SIZE:,} bytes (~{FILE_ROTATION_SIZE/1024/1024:.1f}MB)")
    logger.info(f"Buffer Flush Interval: {BUFFER_FLUSH_INTERVAL}s")
    logger.info(f"Max File Descriptors: {MAX_FILE_DESCRIPTORS}")
    logger.info('=' * 60)

    collector = FreeSwitchLogCollector()
    COLLECTOR = collector

    # register signals AFTER collector is available
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        collector.run()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        try:
            collector.shutdown()
        except Exception:
            pass
        sys.exit(1)


if __name__ == '__main__':
    main()
