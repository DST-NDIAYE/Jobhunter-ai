"""Lance JobHunter AI sur http://127.0.0.1:8000, quel que soit le dossier courant.

Commande : .venv/bin/python run_web.py   (depuis le dossier jobhunter-ai)
"""

import importlib.util
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 8000


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    missing = [m for m in ("fastapi", "uvicorn", "yaml", "fitz", "playwright") if importlib.util.find_spec(m) is None]
    if missing:
        sys.exit(f"Modules absents de ce Python ({sys.executable}) : {', '.join(missing)}.\n"
                 f"Lancez plutôt : {ROOT / '.venv/bin/python'} {ROOT / 'run_web.py'}")
    with socket.socket() as s:
        if s.connect_ex((HOST, PORT)) == 0:
            sys.exit(f"Le port {PORT} est déjà utilisé (un ancien serveur ?). "
                     f"Arrêtez-le : kill $(lsof -tiTCP:{PORT} -sTCP:LISTEN)")
    import uvicorn
    print(f"\nJobHunter AI : ouvrez http://{HOST}:{PORT}  (Ctrl+C pour arrêter)\n")
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True,
                reload_dirs=[str(ROOT / "app"), str(ROOT / "frontend")])


if __name__ == "__main__":
    main()
