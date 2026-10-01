"""Start JARVIS:  python -m jarvis"""
import sys


def main():
    if sys.version_info < (3, 10):
        sys.exit("JARVIS needs Python 3.10 or newer.")
    try:
        from jarvis.ui import run
    except ImportError as e:
        sys.exit(f"A package is missing ({e.name}). Run:  pip install -r jarvis/requirements.txt")
    sys.exit(run())
