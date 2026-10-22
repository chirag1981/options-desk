"""
app/extensions.py — Shared Database & Application Extensions
Centralized location for SQLAlchemy and other global extensions to avoid circular imports.
"""

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
