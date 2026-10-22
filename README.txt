PROJECT HEALTH CHECK

Files:
- project_health_check.py: read-only-by-default checker
- RUN_CHECK.bat: Windows double-click launcher

HOW TO USE
1. Copy both files into the root folder of your Flask project (same folder as app.py/run.py).
2. Double-click RUN_CHECK.bat.
3. Review all FAIL and WARN items.

Terminal usage:
  py -3 project_health_check.py
  py -3 project_health_check.py --report health_report.md

To run pytest as well, first confirm your tests use an isolated test database and do not call live brokers:
  py -3 project_health_check.py --run-tests --report health_report.md

SAFETY AND LIMITATIONS
- Default mode does not import the Flask app, start services, run migrations, or modify source files.
- It checks Python/JSON syntax, optional JavaScript syntax if Node.js exists, dependency presence, and a few review-worthy patterns.
- Optional report output writes only the report file you specify.
- --run-tests may execute code; use only after confirming tests are safe and isolated.
- Pattern scans can have false positives/negatives. PASS does not prove strategy correctness or profitability.
