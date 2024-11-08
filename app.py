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
    LOG_CHECK_TIMEFRAME = int(os.getenv('LOG_CHECK_TIMEFRAME', 5))
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

CORS(app)

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

def check_logs_for_errors(service_name, timeframe_minutes=1):
    """
    Check PM2 logs for error and warning patterns within the specified timeframe
    
    Args:
        service_name (str): Name of the PM2 service
        timeframe_minutes (int): Number of minutes to look back
    """
    try:
        # Get timestamp for time window
        current_time = datetime.now()
        time_window = current_time - timedelta(minutes=timeframe_minutes)
        
        # Get PM2 logs with timestamp included
        cmd = f"{Config.PM2_BIN} logs {service_name} --lines {Config.MAX_LOG_LINES} --nostream --timestamp"
        logs = subprocess.check_output(cmd.split(), universal_newlines=True)
        
        # Parse logs and check for errors/warnings within timeframe
        has_error = False
        has_warning = False
        
        for line in logs.splitlines():
            try:
                # Extract timestamp from log line (assuming PM2 timestamp format)
                # Example format: 2024-11-08T12:32:13: [ERROR] ...
                if ': ' in line:
                    timestamp_str = line.split(': ')[0]
                    log_time = datetime.fromisoformat(timestamp_str)
                    
                    # Only check logs within the timeframe
                    if log_time >= time_window:
                        lower_line = line.lower()
                        if '[error]' in lower_line or 'error' in lower_line:
                            logger.debug(f"Found error in {service_name} at {log_time}: {line}")
                            has_error = True
                        elif '[warning]' in lower_line or 'warn' in lower_line:
                            logger.debug(f"Found warning in {service_name} at {log_time}: {line}")
                            has_warning = True
            except (ValueError, IndexError) as e:
                logger.debug(f"Skipping malformed log line: {line} - {str(e)}")
                continue
        
        if has_error or has_warning:
            logger.warning(
                f"Service {service_name} has errors: {has_error}, "
                f"warnings: {has_warning} in the last {timeframe_minutes} minute(s)"
            )
        
        return has_error, has_warning
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to check logs for {service_name}: {str(e)}")
        return False, False
    except Exception as e:
        logger.error(f"Error checking logs for {service_name}: {str(e)}")
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
                
                # Check logs with explicit timeframe
                has_error, has_warning = check_logs_for_errors(
                    service_name, 
                    timeframe_minutes=Config.LOG_CHECK_TIMEFRAME
                )
                
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
        if conn:
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

# Updated model definitions
status_detail_model = api.model('StatusDetail', {
    'timestamp': fields.DateTime(description='Status timestamp'),
    'status': fields.Integer(description='Status code'),
    'cpu_usage': fields.Float(description='CPU usage percentage'),
    'memory_usage': fields.Float(description='Memory usage (MB)'),
    'has_error': fields.Boolean(description='Error flag'),
    'has_warning': fields.Boolean(description='Warning flag')
})

status_response_model = api.model('StatusResponse', {
    'response_status': fields.String(description='API response status'),
    'timestamp': fields.DateTime(description='Response timestamp'),
    'service_name': fields.String(description='Service name'),
    'data': fields.List(fields.Nested(status_detail_model))
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
    @api.marshal_with(status_response_model)
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
                return api.abort(404, f"Service {service_name} not found")

            status_data = [{
                "timestamp": datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S'),
                "status": row[1],
                "cpu_usage": row[2],
                "memory_usage": row[3],
                "has_error": bool(row[4]),
                "has_warning": bool(row[5])
            } for row in rows]

            return {
                "response_status": "success",
                "timestamp": datetime.now(),
                "service_name": service_name,
                "data": status_data
            }
        except Exception as e:
            logger.error(f"Error getting status for {service_name}: {str(e)}")
            api.abort(500, f"Internal server error: {str(e)}")
        finally:
            if 'conn' in locals():
                conn.close()

# Updated heatmap model
heatmap_data_model = api.model('HeatmapData', {
    'timestamp': fields.DateTime(description='Data point timestamp'),
    'status': fields.String(description='Status color')
})

heatmap_response_model = api.model('HeatmapResponse', {
    'response_status': fields.String(description='API response status'),
    'timestamp': fields.DateTime(description='Response timestamp'),
    'service_name': fields.String(description='Service name'),
    'period': fields.String(description='Time period of data'),
    'data': fields.List(fields.Nested(heatmap_data_model))
})

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
    @api.marshal_with(heatmap_response_model)
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
                return api.abort(404, f"No data found for {service_name}")

            return {
                "response_status": "success",
                "timestamp": datetime.now(),
                "service_name": service_name,
                "period": "72h",
                "data": [{
                    "timestamp": datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S'),
                    "status": row[1]
                } for row in rows]
            }
        except Exception as e:
            logger.error(f"Error getting heatmap for {service_name}: {str(e)}")
            api.abort(500, f"Internal server error: {str(e)}")
        finally:
            if 'conn' in locals():
                conn.close()
                
                
                
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
