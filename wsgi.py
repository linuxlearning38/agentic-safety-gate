import importlib.util, os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

_spec = importlib.util.spec_from_file_location(
    'web_agent',
    os.path.join(os.path.dirname(__file__), 'web_agent_v2.1_guardrail.py')
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
application = _mod.app
