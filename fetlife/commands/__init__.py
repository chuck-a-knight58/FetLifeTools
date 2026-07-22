"""Command implementations live alongside the CLI in :mod:`fetlife.cli`.

This package is reserved for larger, self-contained subcommands as the tool
grows (e.g. bulk exporters or report generators). Keeping it here means new
commands can be dropped in without bloating ``cli.py``.
"""
