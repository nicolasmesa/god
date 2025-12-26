from typing import Annotated

import typer

from god import __version__

app = typer.Typer(
    name="god",
    help="A Virtual Machine Manager CLI",
    no_args_is_help=True,
)


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"god {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", "-v", callback=version_callback, is_eager=True),
    ] = None,
) -> None:
    """A Virtual Machine Manager CLI."""
    pass


if __name__ == "__main__":
    app()
