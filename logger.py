"""
=============================================================================
logger.py - Módulo de Logging Centralizado
=============================================================================
Configura un sistema de logging dual: consola (coloreado) y archivo rotativo.
Usado por todos los demás módulos para registrar eventos del bot.
=============================================================================
"""

import os
import logging
from logging.handlers import RotatingFileHandler


def setup_logger(name: str, log_file: str, level: int = logging.INFO) -> logging.Logger:
    """
    Configura y retorna un logger con salida a consola y archivo rotativo.

    Args:
        name: Nombre del logger (normalmente __name__ del módulo).
        log_file: Ruta al archivo de log.
        level: Nivel de logging (logging.INFO, logging.DEBUG, etc.).

    Returns:
        logging.Logger: Logger configurado y listo para usar.
    """
    # Crear directorio de logs si no existe
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Evitar duplicar handlers si el logger ya fue configurado
    if logger.handlers:
        return logger

    # --- Formato del mensaje de log ---
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # --- Handler para consola (stdout) ---
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    # --- Handler para archivo rotativo (máx 5MB, 3 backups) ---
    file_handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger
