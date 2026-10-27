"""Entry point for the single-file builds (PyInstaller can't use frija/__main__.py directly)."""
from frija.cli import main

if __name__ == "__main__":
    main()
