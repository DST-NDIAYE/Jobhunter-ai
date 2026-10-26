"""Lanceur rapide pour l'application Web moderne JobHunter AI."""

import sys
from pathlib import Path
import uvicorn

if __name__ == "__main__":
    port = 8000
    host = "127.0.0.1"
    print("\n" + "="*60)
    print("🚀  JOBHUNTER AI — NOUVELLE INTERFACE WEB MODERNE")
    print(f"👉  Ouvrez votre navigateur sur : http://{host}:{port}")
    print("="*60 + "\n")
    uvicorn.run("app.main:app", host=host, port=port, reload=True)
