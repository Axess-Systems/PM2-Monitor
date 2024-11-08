import logging
import logging.handlers
import os
from config import Config

def setup_logger(name):
    """Configure logger with rotation and formatting"""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, Config.LOG_LEVEL.upper()))

    # Create logs directory if it doesn't exist
    os.makedirs('logs', exist_ok=True)

    # File Handler with rotation
    file_handler = logging.handlers.RotatingFileHandler(
        filename=os.path.join('logs', Config.LOG_FILE),
        maxBytes=Config.LOG_MAX_BYTES,
        backupCount=Config.LOG_BACKUP_COUNT
    )
    file_handler.setFormatter(logging.Formatter(Config.LOG_FORMAT))
    logger.addHandler(file_handler)

    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(Config.LOG_FORMAT))
    logger.addHandler(console_handler)

    return logger
