from flask import Flask, redirect
from flask_restx import Api, Resource, fields, Namespace
import sqlite3
import subprocess
import json
import re
from datetime import datetime, timedelta
import atexit
import logging
from logging.handlers import RotatingFileHandler
import os
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
class Config:
    PORT = int(os.getenv('PORT', 5000))
    HOST = os.getenv('HOST', '0.0.0.0')
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    DB_PATH = os.getenv('DB_PATH', 'service_status.db')
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FORMAT = os.getenv('LOG_FORMAT', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    LOG_FILE = os.getenv('LOG_FILE', 'logs/log_service.log')
    LOG_MAX_BYTES = int(os.getenv('LOG_MAX_BYTES', 10485760))  # 10MB
    LOG_BACKUP_COUNT = int(os.getenv('LOG_BACKUP_COUNT', 5))
    PM2_BIN = os.getenv('PM2_BIN', 'pm2')
    HIGH_CPU_THRESHOLD = float(os.getenv('HIGH_CPU_THRESHOLD', 80.0))
    HIGH_MEMORY_THRESHOLD = float(os.getenv('HIGH_MEMORY_THRESHOLD', 500.0))  # MB
    LOG_INTERVAL = int(os.getenv('LOG_INTERVAL', 60))  # seconds
    STATUS_RETENTION = int(os.getenv('STATUS_RETENTION', 30))  # days
    MAX_LOG_LINES = int(os.getenv('MAX_LOG_LINES', 100))
    STATUS_COLORS = {
        0: os.getenv('STATUS_COLOR_OFF', "gray"),
        1: os.getenv('STATUS_COLOR_WARNING', "orange"),
        2: os.getenv('STATUS_COLOR_HEALTHY', "green"),
        3: os.getenv('STATUS_COLOR_ERROR', "red")
    }

# Setup logging
os.makedirs('logs', exist_ok=True)
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, Config.LOG_LEVEL))

file_handler = RotatingFileHandler(
    Config.LOG_FILE,
    maxBytes=Config.LOG_MAX_BYTES,
    backupCount=Config.LOG_BACKUP_COUNT
)
file_handler.setFormatter(logging.Formatter(Config.LOG_FORMAT))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(Config.LOG_FORMAT))
logger.addHandler(console_handler)

# Initialize Flask app
app = Flask(__name__)
api = Api(app, 
    version='1.0', 
    title='PM2 Log Service API',
    description='A microservice for monitoring and logging PM2 service statuses',
    doc='/',
    prefix='/api'
)

# Define namespaces
health_ns = api.namespace('health', description='Health checks', path='/api/health')
services_ns = api.namespace('services', description='PM2 services operations', path='/api/services')
status_ns = api.namespace('status', description='Service status operations', path='/api/status')
metrics_ns = api.namespace('metrics', description='Service metrics operations', path='/api/metrics')

# Model definitions
service_model = api.model('Service', {
    'name': fields.String(required=True, description='Service name'),
    'status': fields.String(required=True, description='Service status'),
    'cpu': fields.Float(description='CPU usage percentage'),
    'memory': fields.Float(description='Memory usage (MB)'),
    'uptime': fields.Integer(description='Uptime in seconds')
})

status_model = api.model('Status', {
    'timestamp': fields.DateTime(description='Status timestamp'),
    'status': fields.Integer(description='Status code'),
    'cpu_usage': fields.Float(description='CPU usage percentage'),
    'memory_usage': fields.Float(description='Memory usage (MB)'),
    'has_error': fields.Boolean(description='Error flag'),
    'has_warning': fields.Boolean(description='Warning flag')
})

# Database functions
def setup_database():
    """Initialize SQLite database with required tables"""
    try:
        conn = sqlite3.connect(Config.DB_PATH)
        cursor = conn.cursor()
        
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
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_service_timestamp 
            ON service_status(service_name, timestamp)
        ''')
        
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

# PM2 interaction functions
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

def determine_status(status_str, cpu_usage, memory_usage, has_error, has_warning):
    """Determine service health status"""
    if status_str == "stopped":
        return 0, Config.STATUS_COLORS[0]
    elif has_error:
        return 3, Config.STATUS_COLORS[3]
    elif has_warning or cpu_usage > Config.HIGH_CPU_THRESHOLD or memory_usage > Config.HIGH_MEMORY_THRESHOLD:
        return 1, Config.STATUS_COLORS[1]
    return 2, Config.STATUS_COLORS[2]

def log_status():
    """Log current status of all PM2 services"""
    try:
        conn = sqlite3.connect(Config.DB_PATH)
        cursor = conn.cursor()
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        services = get_all_services()
        if not services:
            logger.warning("No services found to log")
            return

        for service in services:
            try:
                service_name = service.get("name", "Unknown")
                pm2_env = service.get("pm2_env", {})
                monit = service.get("monit", {})
                
                status_str = pm2_env.get("status", "stopped")
                cpu_usage = monit.get("cpu", 0.0)
                memory_usage = monit.get("memory", 0.0) / (1024 * 1024)  # Convert to MB
                
                has_error, has_warning = check_logs_for_errors(service_name)
                status_code, status_color = determine_status(
                    status_str, cpu_usage, memory_usage, has_error, has_warning
                )

                cursor.execute('''
                    INSERT INTO service_status 
                    (service_name, timestamp, status, status_color, cpu_usage, 
                    memory_usage, has_error, has_warning)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (service_name, timestamp, status_code, status_color, 
                    cpu_usage, memory_usage, has_error, has_warning))
                
                logger.info(
                    f"Logged {service_name} - Status: {status_color}, "
                    f"CPU: {cpu_usage}%, Memory: {memory_usage:.1f}MB"
                )
            except Exception as e:
                logger.error(f"Error logging service {service_name}: {str(e)}")
                continue

        conn.commit()
        logger.info(f"Successfully logged status for {len(services)} services")
    except Exception as e:
        logger.error(f"Error in log_status: {str(e)}")
    finally:
        conn.close()

# API Routes
@app.route('/')
def index():
    """Redirect root to Swagger UI"""
    return redirect('/api/')

@health_ns.route('/')
class HealthCheck(Resource):
    @api.doc(responses={200: 'Service is healthy', 500: 'Service is unhealthy'})
    def get(self):
        """Check service health status"""
        return {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "version": "1.0"
        }

@services_ns.route('/')
class ServicesList(Resource):
    @api.doc(responses={200: 'Success', 500: 'Internal server error'})
    def get(self):
        """Get list of all PM2 services"""
        try:
            services = get_all_services()
            formatted_services = []
            
            for service in services:
                pm2_env = service.get('pm2_env', {})
                monit = service.get('monit', {})
                
                formatted_services.append({
                    "name": service.get('name'),
                    "status": pm2_env.get('status', 'unknown'),
                    "cpu": monit.get('cpu', 0),
                    "memory": round(monit.get('memory', 0) / (1024 * 1024), 2),  # Convert to MB
                    "uptime": pm2_env.get('pm_uptime', 0),
                    "restarts": pm2_env.get('restart_time', 0),
                    "pid": pm2_env.get('pid', None),
                    "pm_exec_path": pm2_env.get('pm_exec_path', ''),
                    "created_at": pm2_env.get('created_at', None)
                })

            return {
                "status": "success",
                "timestamp": datetime.now().isoformat(),
                "count": len(formatted_services),
                "services": formatted_services
            }
        except Exception as e:
            logger.error(f"Error getting services: {str(e)}")
            return {
                "status": "error",
                "timestamp": datetime.now().isoformat(),
                "message": str(e)
            }, 500

# Update the service model definition
service_model = api.model('Service', {
    'name': fields.String(required=True, description='Service name'),
    'status': fields.String(required=True, description='Service status'),
    'cpu': fields.Float(description='CPU usage percentage'),
    'memory': fields.Float(description='Memory usage (MB)'),
    'uptime': fields.Integer(description='Uptime in seconds'),
    'restarts': fields.Integer(description='Number of restarts'),
    'pid': fields.Integer(description='Process ID'),
    'pm_exec_path': fields.String(description='Executable path'),
    'created_at': fields.DateTime(description='Creation timestamp')
})

services_response = api.model('ServicesResponse', {
    'status': fields.String(required=True, description='Response status'),
    'timestamp': fields.DateTime(required=True, description='Response timestamp'),
    'count': fields.Integer(required=True, description='Number of services'),
    'services': fields.List(fields.Nested(service_model))
})

@status_ns.route('/<string:service_name>')
class ServiceStatus(Resource):
    @api.doc(
        params={'service_name': 'Name of the PM2 service'},
        responses={
            200: 'Success',
            404: 'Service not found',
            500: 'Internal server error'
        }
    )
    @api.marshal_with(status_model)
    def get(self, service_name):
        """Get status history for a specific service"""
        try:
            conn = sqlite3.connect(Config.DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT timestamp, status, cpu_usage, memory_usage, has_error, has_warning
                FROM service_status 
                WHERE service_name = ? 
                ORDER BY timestamp DESC 
                LIMIT 168
            ''', (service_name,))
            
            rows = cursor.fetchall()
            if not rows:
                return {"status": "error", "message": f"Service {service_name} not found"}, 404

            return {
                "status": "success",
                "service_name": service_name,
                "data": [{
                    "timestamp": row[0],
                    "status": row[1],
                    "cpu_usage": row[2],
                    "memory_usage": row[3],
                    "has_error": bool(row[4]),
                    "has_warning": bool(row[5])
                } for row in rows]
            }
        except Exception as e:
            logger.error(f"Error getting status for {service_name}: {str(e)}")
            return {"status": "error", "message": str(e)}, 500
        finally:
            if 'conn' in locals():
                conn.close()

@status_ns.route('/heatmap/<string:service_name>')
class StatusHeatmap(Resource):
    @api.doc(
        params={'service_name': 'Name of the PM2 service'},
        responses={
            200: 'Success',
            404: 'Service not found',
            500: 'Internal server error'
        }
    )
    def get(self, service_name):
        """Get status heatmap data for the last 72 hours"""
        try:
            conn = sqlite3.connect(Config.DB_PATH)
            cursor = conn.cursor()
            end_time = datetime.now()
            start_time = end_time - timedelta(hours=72)
            
            cursor.execute('''
                SELECT timestamp, status_color
                FROM service_status 
                WHERE service_name = ? 
                AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp ASC
            ''', (service_name, start_time.strftime('%Y-%m-%d %H:%M:%S'), 
                  end_time.strftime('%Y-%m-%d %H:%M:%S')))
            
            rows = cursor.fetchall()
            if not rows:
                return {"status": "error", "message": f"No data found for {service_name}"}, 404

            return {
                "status": "success",
                "service_name": service_name,
                "period": "72h",
                "data": [{
                    "timestamp": row[0],
                    "status": row[1]
                } for row in rows]
            }
        except Exception as e:
            logger.error(f"Error getting heatmap for {service_name}: {str(e)}")
            return {"status": "error", "message": str(e)}, 500
        finally:
            if 'conn' in locals():
                conn.close()

def init_scheduler():
    """Initialize the background scheduler"""
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            func=log_status,
            trigger="interval",
            seconds=Config.LOG_INTERVAL,
            id='log_status',
            name='Log PM2 service status'
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
