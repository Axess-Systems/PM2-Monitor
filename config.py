import os
from datetime import timedelta
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Config:
    # Server Settings
    PORT = int(os.getenv('PORT', 5000))
    HOST = os.getenv('HOST', '0.0.0.0')
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    
    # Database Settings
    DB_PATH = os.getenv('DB_PATH', 'service_status.db')
    
    # Logging Settings
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FORMAT = os.getenv('LOG_FORMAT', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    LOG_FILE = os.getenv('LOG_FILE', 'log_service.log')
    LOG_MAX_BYTES = int(os.getenv('LOG_MAX_BYTES', 10485760))  # 10MB
    LOG_BACKUP_COUNT = int(os.getenv('LOG_BACKUP_COUNT', 5))
    
    # PM2 Settings
    PM2_BIN = os.getenv('PM2_BIN', 'pm2')
    HIGH_CPU_THRESHOLD = float(os.getenv('HIGH_CPU_THRESHOLD', 80.0))
    HIGH_MEMORY_THRESHOLD = float(os.getenv('HIGH_MEMORY_THRESHOLD', 500.0))  # MB
    
    # Service Settings
    LOG_INTERVAL = int(os.getenv('LOG_INTERVAL', 60))  # seconds
    LOG_CHECK_TIMEFRAME = int(os.getenv('LOG_CHECK_TIMEFRAME', 5))
    STATUS_RETENTION = int(os.getenv('STATUS_RETENTION', 30))  # days
    MAX_LOG_LINES = int(os.getenv('MAX_LOG_LINES', 100))
    
    # API Settings
    SWAGGER_UI_DOC_EXPANSION = os.getenv('SWAGGER_UI_DOC_EXPANSION', 'list')
    RESTX_MASK_SWAGGER = os.getenv('RESTX_MASK_SWAGGER', False)
    
    # Status Colors (can be overridden with JSON string in env)
    STATUS_COLORS = {
        0: os.getenv('STATUS_COLOR_OFF', "gray"),
        1: os.getenv('STATUS_COLOR_WARNING', "orange"),
        2: os.getenv('STATUS_COLOR_HEALTHY', "green"),
        3: os.getenv('STATUS_COLOR_ERROR', "red")
    }
