# wsgi.py
# AVA — Gunicorn entrypoint (Day 9)
# Gunicorn imports this file and calls the `application` object.
#
# Usage:
#   gunicorn -c gunicorn.conf.py wsgi:application
#
# The main guardrail file uses:
#   app = Flask(__name__)
# Gunicorn needs the variable named `application`.

import os
from dotenv import load_dotenv

# Load .env before importing the app (JWT_SECRET_KEY must be set)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

import importlib.util, os
_spec = importlib.util.spec_from_file_location(
    'web_agent', os.path.join(os.path.dirname(__file__),
    'web_agent_v2.1_guardrail.py'))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
application = _mod.app  # noqa: F401

# Gunicorn looks for `application` by default
