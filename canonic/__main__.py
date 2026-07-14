"""Entry point for ``python -m canonic`` (used e.g. to relaunch the CLI as a detached subprocess)."""

from canonic.cli.app import app

if __name__ == "__main__":
    app()
