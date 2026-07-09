"""kgi CLI: ingest documents, list pending reviews, resume parked runs with decisions."""

from pathlib import Path

import typer
from rich.console import Console

app = typer.Typer(no_args_is_help=True, help="Knowledge graph ingestion with HITL gating")
console = Console()

_MODALITY_BY_SUFFIX = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".md": "markdown",
    ".bpmn": "bpmn",
}


@app.command()
def ingest(path: Path, thread_id: str = typer.Option(None, help="Resume-able run id")):
    """Run a document through the pipeline up to the review gate (or commit, if fully auto)."""
    from kgi.pipeline import build_pipeline

    modality = _MODALITY_BY_SUFFIX.get(path.suffix.lower())
    if modality is None:
        raise typer.BadParameter(f"unsupported file type: {path.suffix}")

    pipeline = build_pipeline()
    config = {"configurable": {"thread_id": thread_id or path.stem}}
    result = pipeline.invoke({"source_path": str(path), "modality": modality}, config)
    console.print(result)


@app.command()
def pending():
    """List patches parked at the review gate."""
    raise NotImplementedError  # StagingStore.pending_patches()


@app.command()
def review(patch_id: str):
    """Interactive terminal review of one pending patch (v1 stand-in for the web UI)."""
    raise NotImplementedError  # load patch -> show diff -> collect decisions -> Command(resume=)


if __name__ == "__main__":
    app()
