"""Standalone command-line interface."""

import typer

app = typer.Typer(help="Independent BTC backtesting")


@app.callback()
def main() -> None:
    """Run independent BTC backtesting workflows."""
