---
name: verification-agent-standards
description: Enforces evidence-based QA and release gates after code changes in Options Desk.
activation: Always On
---

# Evidence-Based Verification Standard

Before declaring any implementation complete:
1. Run `python scripts/verify_project.py` or `python -m pytest tests/`.
2. Ensure 100% of the 177+ automated tests pass with 0 failures and 0 skipped.
3. Verify broker isolation: paper trading must be strictly enforced.
4. Verify database integrity in `instance/options_signals.db`.
5. Report the release gate verdict: **PASS**, **FAIL**, or **BLOCKED**.
