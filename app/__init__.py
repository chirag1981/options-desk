"""
app/__init__.py — Flask Application Factory
Initializes extensions, registers blueprints, creates database tables,
and manages background scheduler lifecycle.
"""

import os
from flask import Flask
from app.config import Config
from app.extensions import db


def create_app(config_class=Config) -> Flask:
    """Application factory for NIFTY Options Selling Desk."""
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Initialize extensions
    db.init_app(app)

    # Register blueprints
    from app.routes import main_bp
    app.register_blueprint(main_bp)

    # Create tables
    with app.app_context():
        db.create_all()

    # Start background scheduler if not in testing mode
    is_testing = app.config.get("TESTING") or os.environ.get("PYTEST_CURRENT_TEST")
    is_reloader = os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.config.get("DEBUG")

    if not is_testing and is_reloader:
        from app.scheduler import DeskScheduler
        DeskScheduler.start(app, poll_interval_sec=20)

    return app
