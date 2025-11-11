#!/usr/bin/env python3
"""
FreeSWITCH ESL Log Collector - Production Ready
Connects to FreeSWITCH via ESL and organizes logs by domain name
Features:
  - Domain-based log organization
  - Automatic log rotation
  - Buffered writes for performance
  - Comprehensive error handling
  - Health monitoring
  - Graceful shutdown
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

try:
    from freeswitchESL import ESL
except ImportError:
    print("ERROR: ESL module not found. Please ensure it's built and installed.")
    print("      Tried importing: from freeswitchESL import ESL")
    sys.exit(1)

import psutil

# Configuration from environment
ESL_HOST = os.getenv('ESL_HOST', 'localhost')
ESL_PORT = int(os.getenv('ESL_PORT', '8021'))
ESL_PASSWORD = os.getenv('ESL_PASSWORD', 'ClueCon')
LOG_DIR = os.getenv('LOG_DIR', '/var/log/freeswitch-logs')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
RECONNECT_DELAY = int(os.getenv('RECONNECT_DELAY', '5'))
FILE_ROTATION_SIZE = int(os.getenv('FILE_ROTATION_SIZE', '104857600'))  # 100MB
BUFFER_FLUSH_INTERVAL = int(os.getenv('BUFFER_FLUSH_INTERVAL', '5'))  # seconds
MAX_FILE_DESCRIPTORS = int(os.getenv('MAX_FILE_DESCRIPTORS', '50'))

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
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
        """Record event processing"""
        with self.lock:
            self.events_processed += 1
            self.last_event_time = time.time()
    
    def record_write(self, size):
        """Record log write"""
        with self.lock:
            self.logs_written += 1
            self.bytes_written += size
    
    def record_domain(self, count):
        """Update domain count"""
        with self.lock:
            self.domains_count = count
    
    def record_error(self):
        """Record error occurrence"""
        with self.lock:
            self.errors_count += 1
    
    def get_metrics(self):
        """Get current metrics"""
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
            # Fallback to a writable directory when container user lacks permissions.
            fallback = Path('/tmp/freeswitch-logs')
            try:
                fallback.mkdir(parents=True, exist_ok=True)
                logger.warning(f"Permission denied creating {self.log_dir}; falling back to {fallback}")
                self.log_dir = fallback
            except Exception as e:
                logger.error(f"Failed to create fallback log directory {fallback}: {e}")
                raise
        self.rotation_size = rotation_size
        self.file_handles = {}
        self.file_sizes = defaultdict(int)
        self.buffers = defaultdict(list)
        self.lock = Lock()
        self.running = True
        self.metrics = metrics
        
    def extract_domain(self, event, log_line):
        """
        Extract domain name from event headers - much more reliable than log parsing
        Uses domain_name variable (like Lua: session:getVariable("domain_name"))
        Falls back to other headers if domain_name is not available
        
        Also extracts file:line info from LOG events and prepends to output for full context
        """
        if not event:
            return 'unknown'
        
        try:
            # Extract file:line info if available (from LOG events)
            file_line_info = ""
            try:
                file_info = event.getHeader('File')
                line_num = event.getHeader('Line')
                if file_info and line_num:
                    file_line_info = f"[{file_info}:{line_num}] "
            except Exception:
                pass
            
            # Priority 0: Try domain_name variable (like Lua session:getVariable("domain_name"))
            # This is the most direct way to get the domain
            domain = event.getHeader('domain_name')
            if domain and domain != 'default' and domain.strip():
                if self._is_valid_domain(domain):
                    logger.debug(f"Domain from domain_name variable: {domain}")
                    return domain.lower()
            
            # Priority 1: Try Caller-Domain header (most reliable)
            domain = event.getHeader('Caller-Domain')
            if domain and domain != 'default' and domain.strip():
                if self._is_valid_domain(domain):
                    logger.debug(f"Domain from Caller-Domain: {domain}")
                    return domain.lower()
            
            # Priority 2: Try Callee-Domain
            domain = event.getHeader('Callee-Domain')
            if domain and domain != 'default' and domain.strip():
                if self._is_valid_domain(domain):
                    logger.debug(f"Domain from Callee-Domain: {domain}")
                    return domain.lower()
            
            # Priority 3: Try User-Domain variable (FreeSWITCH session variable)
            domain = event.getHeader('User-Domain')
            if domain and domain != 'default' and domain.strip():
                if self._is_valid_domain(domain):
                    logger.debug(f"Domain from User-Domain: {domain}")
                    return domain.lower()
            
            # Priority 4: Try Domain variable (alternative FreeSWITCH variable)
            domain = event.getHeader('Domain')
            if domain and domain != 'default' and domain.strip():
                if self._is_valid_domain(domain):
                    logger.debug(f"Domain from Domain variable: {domain}")
                    return domain.lower()
            
            # Priority 5: Extract from Caller-ID-Number (user@domain)
            caller_id = event.getHeader('Caller-ID-Number')
            if caller_id and '@' in caller_id:
                domain = caller_id.split('@')[1].strip()
                if self._is_valid_domain(domain):
                    logger.debug(f"Domain from Caller-ID-Number: {domain}")
                    return domain.lower()
            
            # Priority 6: From header
            from_hdr = event.getHeader('From')
            if from_hdr:
                domain = self._extract_sip_domain(from_hdr)
                if domain and self._is_valid_domain(domain):
                    logger.debug(f"Domain from From header: {domain}")
                    return domain.lower()
            
            # Priority 7: To header
            to_hdr = event.getHeader('To')
            if to_hdr:
                domain = self._extract_sip_domain(to_hdr)
                if domain and self._is_valid_domain(domain):
                    logger.debug(f"Domain from To header: {domain}")
                    return domain.lower()
            
            # Priority 8: Channel-Name header
            channel = event.getHeader('Channel-Name')
            if channel and '@' in channel:
                domain = channel.split('@')[1].strip()
                if self._is_valid_domain(domain):
                    logger.debug(f"Domain from Channel-Name: {domain}")
                    return domain.lower()
            
            # Priority 9: Try Channel-IP
            ip = event.getHeader('Channel-IP')
            if ip and self._is_valid_domain(ip):
                logger.debug(f"Domain from Channel-IP: {ip}")
                return ip
            
            # Fallback: Try parsing log line
            if log_line and isinstance(log_line, str):
                patterns = [
                    r'sofia/[\w-]+/(?:\w+@)?([\w.-]+)',
                    r'@([\w.-]+)',
                    r'\[([\w.-]+\.\w+)\]',
                ]
                for pattern in patterns:
                    try:
                        match = re.search(pattern, log_line, re.IGNORECASE)
                        if match and match.lastindex >= 1:
                            domain = match.group(1)
                            if domain and self._is_valid_domain(domain):
                                logger.debug(f"Domain from log regex: {domain}")
                                return domain.lower()
                    except Exception:
                        continue
            
            # Last resort: use 'unknown' instead of 'default'
            logger.debug("No domain found in headers or log, using 'unknown'")
            return 'unknown'
            
        except Exception as e:
            logger.error(f"Error extracting domain: {e}")
            return 'unknown'
    
    @staticmethod
    def _extract_sip_domain(sip_string):
        """Extract domain from SIP URI (sip:user@domain)"""
        try:
            match = re.search(r'sip:[\w.+-]*@([\w.-]+)', str(sip_string))
            if match:
                return match.group(1)
        except Exception:
            pass
        return None
    
    @staticmethod
    def _is_valid_domain(domain):
        """Validate domain format"""
        if not domain or len(domain) > 253:
            return False
        
        # Check if it's a valid domain or localhost
        if domain == 'localhost':
            return True
        
        # Must have at least one dot (unless localhost)
        if '.' not in domain:
            return domain.replace('_', '').replace('-', '').isalnum()
        
        # Check domain labels
        labels = domain.split('.')
        for label in labels:
            if not label or len(label) > 63:
                return False
            if not all(c.isalnum() or c in '-_' for c in label):
                return False
        
        return True
    
    def write_log(self, domain, log_line):
        """Write log line to appropriate domain file - REAL-TIME (immediate write)"""
        try:
            with self.lock:
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                formatted_line = f"[{timestamp}] {log_line}\n"
                # Write IMMEDIATELY to file instead of buffering
                self._write_to_file(domain, formatted_line)
        except Exception as e:
            logger.error(f"Error writing log: {e}")
            self.metrics.record_error()
    
    def flush_buffers(self):
        """Flush all buffered logs to disk"""
        try:
            with self.lock:
                flushed_count = 0
                for domain in list(self.buffers.keys()):
                    if self.buffers[domain]:
                        content = ''.join(self.buffers[domain])
                        buffer_size = len(self.buffers[domain])
                        self._write_to_file(domain, content)
                        flushed_count += buffer_size
                        self.buffers[domain].clear()
                
                if flushed_count > 0:
                    logger.debug(f"Flushed {flushed_count} log entries to disk")
                self.metrics.record_domain(len(self.file_handles))
        except Exception as e:
            logger.error(f"Error flushing buffers: {e}")
            self.metrics.record_error()
    
    def _write_to_file(self, domain, content):
        """Write content to domain log file"""
        log_file = self.log_dir / f"{domain}.log"
        
        try:
            # Check if rotation is needed
            if log_file.exists():
                current_size = log_file.stat().st_size
                if current_size + len(content) >= self.rotation_size:
                    self._rotate_log(domain, log_file)
            
            # Ensure we don't exceed max file descriptors
            if len(self.file_handles) >= MAX_FILE_DESCRIPTORS:
                self._close_oldest_file()
            
            # Open file in append mode if not already open
            if domain not in self.file_handles or self.file_handles[domain].closed:
                logger.debug(f"Opening log file: {log_file}")
                self.file_handles[domain] = open(log_file, 'a', buffering=8192)
            
            # Write and flush
            self.file_handles[domain].write(content)
            self.file_handles[domain].flush()
            self.file_sizes[domain] += len(content)
            logger.debug(f"Wrote {len(content)} bytes to {domain}.log (total: {self.file_sizes[domain]} bytes)")
            
        except Exception as e:
            logger.error(f"Error writing to {log_file}: {e}")
            self.metrics.record_error()
            if domain in self.file_handles:
                try:
                    self.file_handles[domain].close()
                except:
                    pass
                del self.file_handles[domain]
    
    def _rotate_log(self, domain, log_file):
        """Rotate log file when it exceeds size limit"""
        try:
            # Close existing handle
            if domain in self.file_handles and not self.file_handles[domain].closed:
                self.file_handles[domain].close()
                del self.file_handles[domain]
            
            # Rename with timestamp
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            rotated_file = self.log_dir / f"{domain}_{timestamp}.log"
            log_file.rename(rotated_file)
            logger.info(f"Rotated log file: {log_file} -> {rotated_file}")
            
            # Reset size counter
            self.file_sizes[domain] = 0
            
        except Exception as e:
            logger.error(f"Error rotating log file {log_file}: {e}")
            self.metrics.record_error()
    
    def _close_oldest_file(self):
        """Close the oldest file to stay within limits"""
        try:
            if self.file_handles:
                oldest_domain = min(self.file_handles.keys(), 
                                   key=lambda d: self.file_sizes.get(d, 0))
                handle = self.file_handles[oldest_domain]
                if not handle.closed:
                    handle.close()
                del self.file_handles[oldest_domain]
                logger.debug(f"Closed oldest file handle for domain: {oldest_domain}")
        except Exception as e:
            logger.error(f"Error closing oldest file: {e}")
    
    def close_all(self):
        """Close all open file handles"""
        self.running = False
        self.flush_buffers()
        with self.lock:
            for domain, handle in self.file_handles.items():
                try:
                    if not handle.closed:
                        handle.close()
                except Exception as e:
                    logger.error(f"Error closing handle for {domain}: {e}")
            self.file_handles.clear()
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
        # Recent event IDs to avoid processing duplicates when multiple subscriptions/formats are used
        self.recent_event_ids = deque(maxlen=10000)
        
    def connect(self):
        """Establish ESL connection to FreeSWITCH"""
        try:
            logger.info(f"Attempting connection to FreeSWITCH at {ESL_HOST}:{ESL_PORT}")
            logger.info(f"Attempt {self.connection_attempts + 1}/{self.max_connection_attempts}")
            
            # Test basic network connectivity first
            import socket
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                result = sock.connect_ex((ESL_HOST, ESL_PORT))
                sock.close()
                if result == 0:
                    logger.debug(f"✓ TCP port {ESL_PORT} is reachable on {ESL_HOST}")
                else:
                    logger.warning(f"✗ TCP port {ESL_PORT} is NOT reachable on {ESL_HOST} (errno: {result})")
                    logger.error(f"Connection refused. Check if FreeSWITCH is running and ESL is enabled.")
                    self.connection_attempts += 1
                    return False
            except socket.gaierror as e:
                logger.error(f"✗ DNS resolution failed for {ESL_HOST}: {e}")
                self.connection_attempts += 1
                return False
            except Exception as e:
                logger.warning(f"Connectivity check error (continuing anyway): {e}")
            
            # Attempt ESL connection
            logger.debug(f"Creating ESL connection with password (length: {len(ESL_PASSWORD)})")
            self.connection = ESL.ESLconnection(ESL_HOST, str(ESL_PORT), ESL_PASSWORD)
            
            if self.connection.connected():
                logger.info("✓ Successfully connected to FreeSWITCH")
                logger.debug(f"Connection object type: {type(self.connection)}")
                self.connection_attempts = 0  # Reset counter on success
                
                # Subscribe to ALL events AND raw logs for complete capture
                # Subscribe to all events first
                self.connection.events("plain", "all")
                logger.info("✓ Subscribed to all events (plain/all)")
                
                # Also enable raw logs to capture everything from fs_cli
                self.connection.filter("Event-Name", "LOG")
                logger.info("✓ Enabled LOG event filtering for raw logs")
                
                return True
            else:
                logger.error("✗ Failed to establish connection (not connected after ESL.ESLconnection())")
                logger.error(f"  - Check credentials: password is {'correct length' if len(ESL_PASSWORD) > 0 else 'empty'}")
                logger.error(f"  - Try: telnet {ESL_HOST} {ESL_PORT}")
                self.connection_attempts += 1
                return False
                
        except Exception as e:
            import traceback
            logger.error(f"✗ Connection error: {type(e).__name__}: {e}")
            logger.debug(f"Traceback: {traceback.format_exc()}")
            self.connection_attempts += 1
            return False
    
    def process_event(self, event):
        """Process incoming ESL event - capture ALL information"""
        try:
            # Attempt to deduplicate events if an identifier header is present
            event_id = None
            try:
                event_id = event.getHeader('Event-Name') + '|' + (event.getHeader('Unique-ID') or event.getHeader('Event-UUID') or '')
            except Exception:
                event_id = None
            if event_id:
                if event_id in self.recent_event_ids:
                    logger.debug(f"Duplicate event ignored: {event_id}")
                    return
                self.recent_event_ids.append(event_id)
            self.metrics.record_event()
            
            event_name = event.getHeader("Event-Name")
            
            # Skip heartbeat events - they're noise
            if event_name == "HEARTBEAT":
                return
            
            # Get log data from various event types
            log_data = None
            
            # Priority 1: Raw LOG events contain the actual fs_cli output
            if event_name == "LOG":
                log_data = event.getBody()
                # For LOG events, also capture the log level and file info
                log_level = event.getHeader("Log-Level") or "INFO"
                if log_data:
                    # Preserve raw format but add context
                    log_data = f"[{log_level}] {log_data}"
                    
            # Priority 2: CHANNEL_LOG events
            elif event_name == "CHANNEL_LOG":
                log_data = event.getBody()
                
            # Priority 3: All other events (except HEARTBEAT)
            elif event_name and event_name != "HEARTBEAT":
                # For other events, create a formatted log entry with full context
                priority = event.getHeader("Log-Level") or "INFO"
                body = event.getBody() or ""
                
                # Try to extract call information
                uuid = event.getHeader('Unique-ID') or event.getHeader('Channel-Unique-ID') or ""
                channel_name = event.getHeader('Channel-Name') or ""
                caller_id_name = event.getHeader('Caller-ID-Name') or ""
                caller_id_number = event.getHeader('Caller-ID-Number') or ""
                
                # Build rich log entry
                if uuid or channel_name:
                    call_info = f"({channel_name or uuid})"
                else:
                    call_info = ""
                    
                if caller_id_number:
                    call_info += f" [{caller_id_name or caller_id_number}]"
                
                log_data = f"[{event_name}] [{priority}] {call_info} {body}".strip()
            
            if log_data:
                # Extract domain from event headers + log line and write immediately
                domain = self.log_manager.extract_domain(event, log_data)
                self.log_manager.write_log(domain, log_data)
                
        except Exception as e:
            logger.error(f"Error processing event: {e}")
            self.metrics.record_error()
    
    def run(self):
        """Main run loop"""
        self.running = True
        logger.info("Starting main collection loop...")
        
        while self.running:
            try:
                if not self.connection or not self.connection.connected():
                    if self.connection_attempts >= self.max_connection_attempts:
                        logger.error(f"Failed to connect after {self.max_connection_attempts} attempts")
                        logger.error("Waiting 30 seconds before retry...")
                        for i in range(6):
                            if not self.running:
                                break
                            time.sleep(5)
                        self.connection_attempts = 0
                        continue
                    
                    logger.warning("Connection lost, attempting to reconnect...")
                    if not self.connect():
                        logger.error(f"Reconnection failed, retrying in {RECONNECT_DELAY}s")
                        time.sleep(RECONNECT_DELAY)
                        continue
                
                # Receive events with timeout
                try:
                    event = self.connection.recvEventTimed(1000)  # 1 second timeout
                    
                    if event:
                        self.process_event(event)
                except Exception as e:
                    logger.warning(f"Error receiving event: {e}")
                
                # Periodic buffer flush
                current_time = time.time()
                if current_time - self.last_flush >= BUFFER_FLUSH_INTERVAL:
                    self.log_manager.flush_buffers()
                    self.last_flush = current_time
                    
                    # Log metrics periodically
                    if int(current_time) % 60 == 0:  # Every 60 seconds
                        metrics = self.metrics.get_metrics()
                        logger.info(f"Metrics: {json.dumps(metrics)}")
                        
            except KeyboardInterrupt:
                logger.info("Received interrupt signal")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                self.metrics.record_error()
                time.sleep(1)
        
        self.shutdown()
    
    def shutdown(self):
        """Clean shutdown"""
        logger.info("Shutting down collector...")
        self.running = False
        
        try:
            if self.connection and self.connection.connected():
                self.connection.disconnect()
                logger.info("Disconnected from FreeSWITCH")
        except Exception as e:
            logger.error(f"Error disconnecting: {e}")
        
        self.log_manager.close_all()
        
        # Final metrics
        metrics = self.metrics.get_metrics()
        logger.info(f"Final metrics: {json.dumps(metrics)}")
        logger.info("Shutdown complete")

    def get_metrics(self):
        """Get current metrics (for health check)"""
        return self.metrics.get_metrics()

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, initiating shutdown...")
    sys.exit(0)


def main():
    """Main entry point"""
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("=" * 60)
    logger.info("FreeSWITCH Log Collector - Production Ready")
    logger.info("=" * 60)
    logger.info(f"ESL Host: {ESL_HOST}:{ESL_PORT}")
    logger.info(f"Log Directory: {LOG_DIR}")
    logger.info(f"File Rotation Size: {FILE_ROTATION_SIZE:,} bytes (~{FILE_ROTATION_SIZE/1024/1024:.1f}MB)")
    logger.info(f"Buffer Flush Interval: {BUFFER_FLUSH_INTERVAL}s")
    logger.info(f"Max File Descriptors: {MAX_FILE_DESCRIPTORS}")
    logger.info("=" * 60)
    
    collector = FreeSwitchLogCollector()
    
    try:
        collector.run()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        collector.shutdown()
        sys.exit(1)

if __name__ == "__main__":
    main()