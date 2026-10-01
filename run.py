"""
run.py — Server entry point for Options Desk
"""

import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Run in threaded mode for responsive options data serving
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
