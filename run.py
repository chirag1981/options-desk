"""
run.py — Server entry point for Options Desk
"""

import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() in ("true", "1", "yes")
    # Run in threaded mode for responsive options data serving
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
