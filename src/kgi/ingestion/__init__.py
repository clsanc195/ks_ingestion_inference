from kgi.ingestion.base import Parser, get_parser, register_parser

__all__ = ["Parser", "get_parser", "register_parser"]

# Importing registers the built-in parsers.
from kgi.ingestion import bpmn as _bpmn  # noqa: E402,F401
from kgi.ingestion import markdown as _markdown  # noqa: E402,F401

try:  # docling is an optional extra: pip install kgi[parsers]
    from kgi.ingestion import docling_parser as _docling  # noqa: E402,F401
except ImportError:
    pass
