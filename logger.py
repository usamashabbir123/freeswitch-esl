#!/usr/bin/env python3
"""
FreeSWITCH ESL Log Collector
Connects to FreeSWITCH via ESL and organizes logs by domain name
"""

import os
import sys
import time
import signal
import logging
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from threading import Lock
import re

import ESL

# Configuration
ESL_HOST = os.getenv('ESL_HOST', 'localhost')
ESL_PORT = int(os.getenv('ESL_PORT', '8021'))
ESL_PASSWORD = os.getenv('ESL_PASSWORD', 'ClueCon')
LOG_DIR = os.getenv('LOG_DIR', '/var/log/freeswitch-logs')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
RECONNECT_DELAY = int(os.getenv('RECONNECT_DELAY', '5'))
FILE_ROTATION_SIZE = int(os.getenv('FILE_ROTATION_SIZE', '104857600'))  # 100MB
BUFFER_FLUSH_INTERVAL = int(os.getenv('BUFFER_FLUSH_INTERVAL', '5'))  # seconds

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('freeswitch-logger')

class LogManager:
    """Manages log files for different domains with buffering and rotation"""
    
    def __init__(self, log_dir, rotation_size):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.rotation_size = rotation_size
        self.file_handles = {}
        self.file_sizes = defaultdict(int)
        self.buffers = defaultdict(list)
        self.lock = Lock()
        self.running = True
        
    def extract_domain(self, log_line):
        """Extract domain name from log line"""
        # Common FreeSWITCH domain patterns
        patterns = [
            r'sofia/[\w-]+/([\w.-]+@)?([a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z0-9][-a-zA-Z0-9.]*)',
            r'\[(\w+\.[\w.-]+)\]',
            r'domain[=:](\w+\.[\w.-]+)',
            r'@([a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z0-9][-a-zA-Z0-9.]*)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, log_line)
            if match:
                # Return the domain part (last group that looks like a domain)
                domain = match.group(match.lastindex)
                if '.' in domain:
                    return domain
        
        return 'default'
    
    def write_log(self, domain, log_line):
        """Write log line to appropriate domain file with buffering"""
        with self.lock:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            formatted_line = f"[{timestamp}] {log_line}\n"
            self.buffers[domain].append(formatted_line)
    
    def flush_buffers(self):
        """Flush all buffered logs to disk"""
        with self.lock:
            for domain, lines in self.buffers.items():
                if lines:
                    self._write_to_file(domain, ''.join(lines))
                    self.buffers[domain].clear()
    
    def _write_to_file(self, domain, content):
        """Write content to domain log file"""
        log_file = self.log_dir / f"{domain}.log"
        
        try:
            # Check if rotation is needed
            if log_file.exists() and log_file.stat().st_size >= self.rotation_size:
                self._rotate_log(domain, log_file)
            
            # Open file in append mode if not already open
            if domain not in self.file_handles or self.file_handles[domain].closed:
                self.file_handles[domain] = open(log_file, 'a', buffering=8192)
            
            self.file_handles[domain].write(content)
            self.file_handles[domain].flush()
            self.file_sizes[domain] += len(content)
            
        except Exception as e:
            logger.error(f"Error writing to {log_file}: {e}")
    
    def _rotate_log(self, domain, log_file):
        """Rotate log file when it exceeds size limit"""
        try:
            # Close existing handle
            if domain in self.file_handles and not self.file_handles[domain].closed:
                self.file_handles[domain].close()
            
            # Rename with timestamp
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            rotated_file = self.log_dir / f"{domain}_{timestamp}.log"
            log_file.rename(rotated_file)
            logger.info(f"Rotated log file: {log_file} -> {rotated_file}")
            
            # Reset size counter
            self.file_sizes[domain] = 0
            
        except Exception as e:
            logger.error(f"Error rotating log file {log_file}: {e}")
    
    def close_all(self):
        """Close all open file handles"""
        self.running = False
        self.flush_buffers()
        with self.lock:
            for handle in self.file_handles.values():
                if not handle.closed:
                    handle.close()
            self.file_handles.clear()

class FreeSwitchLogCollector:
    """Main collector class that connects to FreeSWITCH via ESL"""
    
    def __init__(self):
        self.connection = None
        self.log_manager = LogManager(LOG_DIR, FILE_ROTATION_SIZE)
        self.running = False
        self.last_flush = time.time()
        
    def connect(self):
        """Establish ESL connection to FreeSWITCH"""
        try:
            logger.info(f"Connecting to FreeSWITCH at {ESL_HOST}:{ESL_PORT}")
            self.connection = ESL.ESLconnection(ESL_HOST, str(ESL_PORT), ESL_PASSWORD)
            
            if self.connection.connected():
                logger.info("Successfully connected to FreeSWITCH")
                
                # Subscribe to all log events
                self.connection.events("plain", "all")
                logger.info("Subscribed to all events")
                
                return True
            else:
                logger.error("Failed to connect to FreeSWITCH")
                return False
                
        except Exception as e:
            logger.error(f"Connection error: {e}")
            return False
    
    def process_event(self, event):
        """Process incoming ESL event"""
        try:
            event_name = event.getHeader("Event-Name")
            
            # Get log data from various event types
            log_data = None
            
            if event_name == "LOG":
                log_data = event.getBody()
            elif event_name:
                # For other events, create a formatted log entry
                log_data = f"[{event_name}] {event.serialize()}"
            
            if log_data:
                # Extract domain and write log
                domain = self.log_manager.extract_domain(log_data)
                self.log_manager.write_log(domain, log_data)
                
        except Exception as e:
            logger.error(f"Error processing event: {e}")
    
    def run(self):
        """Main run loop"""
        self.running = True
        
        while self.running:
            try:
                if not self.connection or not self.connection.connected():
                    logger.warning("Connection lost, reconnecting...")
                    if not self.connect():
                        logger.error(f"Reconnection failed, waiting {RECONNECT_DELAY}s")
                        time.sleep(RECONNECT_DELAY)
                        continue
                
                # Receive events with timeout
                event = self.connection.recvEventTimed(1000)  # 1 second timeout
                
                if event:
                    self.process_event(event)
                
                # Periodic buffer flush
                current_time = time.time()
                if current_time - self.last_flush >= BUFFER_FLUSH_INTERVAL:
                    self.log_manager.flush_buffers()
                    self.last_flush = current_time
                    
            except KeyboardInterrupt:
                logger.info("Received interrupt signal")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                time.sleep(1)
        
        self.shutdown()
    
    def shutdown(self):
        """Clean shutdown"""
        logger.info("Shutting down...")
        self.running = False
        
        if self.connection and self.connection.connected():
            self.connection.disconnect()
            logger.info("Disconnected from FreeSWITCH")
        
        self.log_manager.close_all()
        logger.info("Shutdown complete")

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}")
    sys.exit(0)

def main():
    """Main entry point"""
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("FreeSWITCH Log Collector starting...")
    logger.info(f"ESL Host: {ESL_HOST}:{ESL_PORT}")
    logger.info(f"Log Directory: {LOG_DIR}")
    logger.info(f"File Rotation Size: {FILE_ROTATION_SIZE} bytes")
    
    collector = FreeSwitchLogCollector()
    
    try:
        collector.run()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        collector.shutdown()
        sys.exit(1)

if __name__ == "__main__":
    main()