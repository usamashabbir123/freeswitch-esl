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
from collections import defaultdict
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
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.rotation_size = rotation_size
        self.file_handles = {}
        self.file_sizes = defaultdict(int)
        self.buffers = defaultdict(list)
        self.lock = Lock()
        self.running = True
        self.metrics = metrics
        
    def extract_domain(self, log_line):
        """
        Extract domain name from log line
        Supports multiple FreeSWITCH log formats
        """
        if not isinstance(log_line, str):
            return 'default'
        
        # Common FreeSWITCH domain patterns
        patterns = [
            # Sofia SIP domain (e.g., "sofia/internal/user@domain.com")
            r'sofia/[\w-]+/(?:\w+@)?([\w.-]+)',
            # Generic domain in brackets [domain.com]
            r'\[([\w.-]+\.\w+)\]',
            # Domain= or domain: formats
            r'domain[=:]\s*([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z0-9][-a-zA-Z0-9]*)*)',
            # Email-like domain (@domain.com)
            r'@([\w.-]+\.\w+)',
            # From/To headers (sip:user@domain.com)
            r'sip:[\w.+-]*@([\w.-]+)',
            # X-header domain
            r'domain\s+([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z0-9][-a-zA-Z0-9]*)*)',
        ]
        
        for pattern in patterns:
            try:
                match = re.search(pattern, log_line, re.IGNORECASE)
                if match:
                    domain = match.group(1) if match.lastindex >= 1 else None
                    if domain:
                        # Validate domain format
                        if self._is_valid_domain(domain):
                            return domain.lower()
            except Exception as e:
                logger.debug(f"Error in regex pattern: {e}")
                continue
        
        return 'default'
    
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
        """Write log line to appropriate domain file with buffering"""
        try:
            with self.lock:
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                formatted_line = f"[{timestamp}] {log_line}\n"
                self.buffers[domain].append(formatted_line)
                self.metrics.record_write(len(formatted_line))
        except Exception as e:
            logger.error(f"Error writing log: {e}")
            self.metrics.record_error()
    
    def flush_buffers(self):
        """Flush all buffered logs to disk"""
        try:
            with self.lock:
                for domain in list(self.buffers.keys()):
                    if self.buffers[domain]:
                        content = ''.join(self.buffers[domain])
                        self._write_to_file(domain, content)
                        self.buffers[domain].clear()
                
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
                self.file_handles[domain] = open(log_file, 'a', buffering=8192)
            
            # Write and flush
            self.file_handles[domain].write(content)
            self.file_handles[domain].flush()
            self.file_sizes[domain] += len(content)
            
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
                
                # Subscribe to all log events
                self.connection.events("plain", "all")
                logger.info("✓ Subscribed to all events")
                
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
        """Process incoming ESL event"""
        try:
            self.metrics.record_event()
            
            event_name = event.getHeader("Event-Name")
            
            # Get log data from various event types
            log_data = None
            
            if event_name == "LOG":
                log_data = event.getBody()
            elif event_name == "CHANNEL_LOG":
                log_data = event.getBody()
            elif event_name and event_name != "HEARTBEAT":
                # For other events, create a formatted log entry
                priority = event.getHeader("Log-Level") or "INFO"
                body = event.getBody() or ""
                log_data = f"[{event_name}] [{priority}] {body}"
            
            if log_data:
                # Extract domain and write log
                domain = self.log_manager.extract_domain(log_data)
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