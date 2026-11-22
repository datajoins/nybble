"""Allow ``python -m nybble`` to invoke the CLI.

    python -m nybble serve
    python -m nybble query "SELECT 1"
    python -m nybble --help
"""
from nybble.cli import cli

if __name__ == "__main__":
    cli()
