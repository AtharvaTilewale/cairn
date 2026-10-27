"""Cairn command-line interface."""

import typer

app = typer.Typer(help="Cairn: the lab notebook that writes itself.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Cairn: the lab notebook that writes itself."""
