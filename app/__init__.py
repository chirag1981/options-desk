"""
app/__init__.py — Application Factory for Fyers Option Desk
"""

import os
from datetime import timedelta
from flask import Flask, render_template
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()


def create_app(config_name: str = None) -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")

    # Configuration
    secret_key = os.environ.get("SECRET_KEY", "fyers_option_desk_secret_key_2026_super_secure")
    app.config["SECRET_KEY"] = secret_key
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
    app.config["SESSION_COOKIE_SECURE"] = False  # Relaxed for local dev
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # Initialize extensions
    csrf.init_app(app)

    # Register Option Desk Blueprint
    from app.routes.options_desk import options_desk_bp
    app.register_blueprint(options_desk_bp)

    # Start background 3-minute options updater
    try:
        from app.services.options_scheduler import start_options_scheduler
        start_options_scheduler(app)
    except Exception:
        pass

    # Error handlers
    @app.errorhandler(404)
    def page_not_found(e):
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def internal_server_error(e):
        return render_template("500.html"), 500

    return app
