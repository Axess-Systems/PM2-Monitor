# app.py

from flask import Flask
from flask_restx import Api, Resource, fields, Namespace
import sqlite3
import subprocess
import json
import re
from datetime import datetime, timedelta
import atexit
from apscheduler.schedulers.background import BackgroundScheduler
from config import Config
from logger import setup_logger

# Initialize logger
logger = setup_logger(__name__)

app = Flask(__name__)
api = Api(app, 
    version='1.0', 
    title='PM2 Log Service API',
    description='A microservice for monitoring and logging PM2 service statuses',
    doc='/docs'
)

# Define namespaces
health_ns = api.namespace('health', description='Health checks')
services_ns = api.namespace('services', description='PM2 services operations')
status_ns = api.namespace('status', description='Service status operations')
metrics_ns = api.namespace('metrics', description='Service metrics operations')

# Model definitions remain the same...

def setup_database():
    """Initialize SQLite database with required tables"""
    try:
        conn = sqlite3.connect(Config.DB_PATH)
        cursor = conn.cursor()
        
        # Create tables
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS service_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_name TEXT,
                timestamp TEXT,
                status INTEGER,
                status_color TEXT,
                cpu_usage REAL,
                memory_usage REAL,
                has_error BOOLEAN DEFAULT 0,
                has_warning BOOLEAN DEFAULT 0
            )
        ''')
        
        # Create indexes
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_service_timestamp 
            ON service_status(service_name, timestamp)
        ''')
        
        # Create cleanup trigger
        cursor.execute(f'''
            CREATE TRIGGER IF NOT EXISTS cleanup_old_status
            AFTER INSERT ON service_status
            BEGIN
                DELETE FROM service_status 
                WHERE timestamp <= datetime('now', '-{Config.STATUS_RETENTION} days');
            END
        ''')
        
        conn.commit()
        logger.info("Database setup completed successfully")
    except Exception as e:
        logger.error(f"Database setup failed: {str(e)}")
        raise
    finally:
        conn.close()

def get_all_services():
    """Retrieve list of PM2 services and their status"""
    try:
        cmd = f"{Config.PM2_BIN} jlist"
        output = subprocess.check_output(cmd.split(), universal_newlines=True)
        services = json.loads(output)
        logger.info(f"Retrieved {len(services)} services from PM2")
        return services
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to execute PM2 command: {str(e)}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse PM2 output: {str(e)}")
        return []

def check_logs_for_errors(service_name):
    """Check PM2 logs for error and warning patterns"""
    try:
        cmd = f"{Config.PM2_BIN} logs {service_name} --lines {Config.MAX_LOG_LINES} --nostream"
        logs = subprocess.check_output(cmd.split(), universal_newlines=True)
        has_error = bool(re.search(r'\[ERROR\]|\berror\b', logs, re.IGNORECASE))
        has_warning = bool(re.search(r'\[WARNING\]|\bwarn\b', logs, re.IGNORECASE))
        
        if has_error or has_warning:
            logger.warning(f"Service {service_name} has errors: {has_error}, warnings: {has_warning}")
        
        return has_error, has_warning
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to check logs for {service_name}: {str(e)}")
        return False, False

# Rest of the endpoint implementations...

def cleanup_old_data():
    """Cleanup data older than retention period"""
    try:
        conn = sqlite3.connect(Config.DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute(f'''
            DELETE FROM service_status 
            WHERE timestamp <= datetime('now', '-{Config.STATUS_RETENTION} days')
        ''')
        
        deleted_count = cursor.rowcount
        conn.commit()
        logger.info(f"Cleaned up {deleted_count} old status records")
    except Exception as e:
        logger.error(f"Failed to cleanup old data: {str(e)}")
    finally:
        conn.close()

def init_scheduler():
    """Initialize the background scheduler"""
    try:
        scheduler = BackgroundScheduler()
        
        # Add status logging job
        scheduler.add_job(
            func=log_status,
            trigger="interval",
            seconds=Config.LOG_INTERVAL,
            id='log_status',
            name='Log PM2 service status'
        )
        
        # Add cleanup job (runs daily)
        scheduler.add_job(
            func=cleanup_old_data,
            trigger="interval",
            hours=24,
            id='cleanup_data',
            name='Clean up old status data'
        )
        
        scheduler.start()
        atexit.register(lambda: scheduler.shutdown())
        logger.info("Scheduler initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize scheduler: {str(e)}")
        raise

if __name__ == '__main__':
    try:
        logger.info("Starting PM2 Log Service")
        setup_database()
        init_scheduler()
        app.run(
            host=Config.HOST,
            port=Config.PORT,
            debug=Config.DEBUG
        )
    except Exception as e:
        logger.error(f"Service startup failed: {str(e)}")
        raise
