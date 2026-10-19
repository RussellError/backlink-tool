"""Launcher / helper. `python main.py` starts the app (same as `python app.py`).

  python main.py            start the web app on 127.0.0.1:8080
  python main.py --check    verify Python version, packages, folders and database
"""
import importlib
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))


def check():
    ok = True
    if sys.version_info < (3, 8):
        print("Python 3.8 or newer is required"); ok = False
    for pkg in ("flask", "requests", "bs4"):
        try:
            importlib.import_module(pkg)
        except ImportError:
            print(f"Missing package: {pkg}. Run: python -m pip install -r requirements.txt"); ok = False
    for d in ("database", "reports", "logs", "data"):
        os.makedirs(os.path.join(BASE, d), exist_ok=True)
    if ok:
        sys.path.insert(0, BASE)
        from modules import db
        db.init_db()
        print("OK: packages found, folders ready, database initialised at", db.DB_PATH)
    return ok


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0 if check() else 1)
    if not check():
        sys.exit(1)
    sys.path.insert(0, BASE)
    import runpy
    runpy.run_path(os.path.join(BASE, "app.py"), run_name="__main__")
