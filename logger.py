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
from datetime import datetime
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
        if not event:
            return 'unknown'
        try:
            for header in ('domain_name', 'Caller-Domain', 'Callee-Domain', 'User-Domain', 'Domain'):
                try:
                    v = event.getHeader(header)
                except Exception:
                    v = None
                if v and v.strip() and v.lower() != 'default':
                    if self._is_valid_domain(v):
                        return v.lower()

            # Try parsing common SIP headers
            try:
                caller_id = event.getHeader('Caller-ID-Number')
            except Exception:
                caller_id = None
            if caller_id and '@' in caller_id:
                domain = caller_id.split('@', 1)[1].strip()
                if self._is_valid_domain(domain):
                    return domain.lower()

            for hdr in ('From', 'To', 'Channel-Name'):
                try:
                    h = event.getHeader(hdr)
                except Exception:
                    h = None
                if h:
                    dm = self._extract_sip_domain(h)
                    if dm and self._is_valid_domain(dm):
                        return dm.lower()

            # Fallback: regex against log_line
            if log_line and isinstance(log_line, str):
                patterns = [r'sofia/[\w-]+/(?:[\w%+\-]+@)?([\w.-]+)', r'@([\w.-]+)', r'\[([\w.-]+\.[a-zA-Z]{2,})\]']
                for p in patterns:
                    m = re.search(p, log_line)
                    if m:
                        d = m.group(1)
                        if self._is_valid_domain(d):
                            return d.lower()

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
                timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
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

            timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
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
                try:
                    self.connection.events('plain', 'all')
                except Exception:
                    try:
                        self.connection.events('log', 'all')
                    except Exception:
                        logger.debug('Could not subscribe to events via provided API')
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
                    log_data = event.getBody() or ''
                except Exception:
                    log_data = ''
            elif event_name and event_name.upper() != 'HEARTBEAT':
                priority = event.getHeader('Log-Level') or 'INFO'
                body = event.getBody() or ''
                log_data = f"[{event_name}] [{priority}] {body}"

            if log_data:
                domain = self.log_manager.extract_domain(event, log_data)
                self.log_manager.write_log(domain, log_data)
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
