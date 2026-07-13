"""kgi CLI: ingest documents, list pending reviews, review parked patches.

Runs are durable (Postgres checkpointer): `kgi ingest` can park at the review gate,
the process exits, and `kgi review` resumes the same run days later.
"""

import getpass
import uuid
from pathlib import Path

import typer
from langgraph.types import Command
from rich.console import Console
from rich.table import Table

app = typer.Typer(no_args_is_help=True, help="Knowledge graph ingestion with HITL gating")
console = Console()

_MODALITY_BY_SUFFIX = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".md": "markdown",
    ".bpmn": "bpmn",
}


def _print_report(result: dict) -> None:
    report = result.get("commit_report", {})
    if "duplicate_document" in report:
        console.print(f"[yellow]duplicate document — already ingested "
                      f"({report['duplicate_document']}), nothing to do[/yellow]")
        return
    if "document_in_flight" in report:
        console.print(f"[yellow]document already staged and awaiting review "
                      f"(patch {report['document_in_flight']}) — decide it with "
                      f"'kgi review' or the board before re-ingesting[/yellow]")
        return
    console.print(
        f"[green]committed {len(report.get('committed', []))}[/green] · "
        f"requeued {len(report.get('requeued', []))} · "
        f"blocked {len(report.get('blocked', []))} · "
        f"skipped {len(report.get('skipped', []))}"
    )


@app.command()
def ingest(path: Path, thread_id: str = typer.Option(None, help="Resume-able run id")):
    """Run a document through the pipeline up to the review gate (or commit, if fully auto)."""
    from kgi.pipeline import durable_pipeline

    modality = _MODALITY_BY_SUFFIX.get(path.suffix.lower())
    if modality is None:
        raise typer.BadParameter(f"unsupported file type: {path.suffix}")

    from kgi import observability

    thread = thread_id or f"ingest_{uuid.uuid4().hex[:10]}"
    with durable_pipeline() as pipeline:
        config = {
            "configurable": {"thread_id": thread},
            "callbacks": observability.langgraph_callbacks(),
            "metadata": {"langfuse_session_id": thread, "source": str(path)},
        }
        # One root span per command: the LangGraph node spans AND the LLM
        # generations inside the nodes all nest under it -> one trace per ingest.
        with observability.span(f"kgi-ingest {path.name}", {"path": str(path)}) as sp:
            result = pipeline.invoke(
                {"source_path": str(path), "modality": modality}, config
            )
            if sp is not None:
                sp.update(output={"parked": "__interrupt__" in result,
                                  "report": result.get("commit_report")})
    if "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        console.print(
            f"[bold]parked at review gate[/bold] — patch [cyan]{payload['patch_id']}[/cyan] "
            f"with {len(payload['ops'])} ops awaiting review"
        )
        console.print(f"next: [bold]kgi review {payload['patch_id']}[/bold]")
    else:
        _print_report(result)


@app.command()
def pending():
    """List patches parked at the review gate."""
    from kgi.models import OpStatus, RouteTarget
    from kgi.stores.neo4j import StagingStore

    staging = StagingStore()
    try:
        patches = staging.pending_patches()
    finally:
        staging.close()
    if not patches:
        console.print("review queue is empty")
        return
    table = Table("patch", "document", "created", "ops awaiting review")
    for p in patches:
        awaiting = sum(
            1 for r in p.ops
            if r.route == RouteTarget.review and r.status == OpStatus.pending
        )
        table.add_row(p.patch_id, p.doc_id, p.created_at.strftime("%Y-%m-%d %H:%M"),
                      str(awaiting))
    console.print(table)


def _show_op(index: int, total: int, routed) -> None:
    op = routed.op
    console.rule(f"op {index}/{total} · [bold]{op.op}[/bold] · confidence {op.confidence:.2f}")
    console.print(f"[italic]{op.rationale}[/italic]")
    payload = op.model_dump(exclude={"op_id", "depends_on", "preconditions",
                                     "confidence", "rationale"})
    for key, value in payload.items():
        if value not in (None, {}, []):
            console.print(f"  {key}: {value}")


@app.command()
def review(
    patch_id: str,
    reviewer: str = typer.Option(None, help="Reviewer id recorded in provenance"),
    decide_all: str = typer.Option(
        None, "--all", help="Non-interactive: apply one action (accept|reject) to every op"
    ),
):
    """Review a parked patch op-by-op ([a]ccept / [r]eject / [d]efer), then resume the run."""
    from kgi.models import OpStatus, RouteTarget
    from kgi.pipeline import durable_pipeline
    from kgi.stores.neo4j import StagingStore

    reviewer = reviewer or getpass.getuser()
    staging = StagingStore()
    try:
        patch = staging.load_patch(patch_id)
        thread = staging.thread_for_patch(patch_id)
    finally:
        staging.close()
    if thread is None:
        raise typer.BadParameter(f"patch {patch_id} has no parked run to resume")

    reviewable = [
        r for r in patch.ops
        if r.route == RouteTarget.review and r.status == OpStatus.pending
    ]
    if not reviewable:
        console.print("nothing awaiting review on this patch")
        raise typer.Exit()

    decisions: dict[str, dict] = {}
    if decide_all:
        if decide_all not in ("accept", "reject"):
            raise typer.BadParameter("--all must be accept or reject")
        decisions = {
            r.op.op_id: {"action": decide_all, "reviewer_id": reviewer}
            for r in reviewable
        }
        console.print(f"{decide_all}ing all {len(reviewable)} ops as {reviewer!r}")
    else:
        console.print(f"[bold]{len(reviewable)} ops[/bold] on patch {patch_id} "
                      f"(reviewer: {reviewer})")
        for i, routed in enumerate(reviewable, 1):
            _show_op(i, len(reviewable), routed)
            choice = typer.prompt("  [a]ccept / [r]eject / [d]efer", default="d").lower()
            if choice.startswith("a"):
                decisions[routed.op.op_id] = {"action": "accept", "reviewer_id": reviewer}
            elif choice.startswith("r"):
                note = typer.prompt("  note (why)", default="")
                decisions[routed.op.op_id] = {
                    "action": "reject", "reviewer_id": reviewer, "note": note,
                }
            # defer: no decision recorded; op stays pending and is held back at commit

    from kgi import observability

    with durable_pipeline() as pipeline:
        config = {
            "configurable": {"thread_id": thread},
            "callbacks": observability.langgraph_callbacks(),
            "metadata": {"langfuse_session_id": thread, "patch_id": patch_id},
        }
        with observability.span(f"kgi-review {patch_id}",
                                {"decisions": len(decisions)}) as sp:
            result = pipeline.invoke(Command(resume=decisions), config)
            if sp is not None:
                sp.update(output=result.get("commit_report"))
    _print_report(result)


@app.command()
def eval(
    bench_dir: str = typer.Option("examples/benchmarks/webnlg",
                                  help="Directory of *.md docs with *.gold.json sidecars"),
    pred_threshold: float = typer.Option(0.60,
                                         help="Predicate embedding-similarity bar for a full-triple match"),
):
    """Extraction eval vs gold triples — DB-free; gates prompt/model changes.

    pair = right subject & object · triple = pair + similar predicate."""
    from kgi.evaluation import run_extraction_eval

    report = run_extraction_eval(bench_dir, pred_threshold)
    agg = report["aggregate"]
    console.print(f"\n[bold]extraction eval[/bold] · model {report['model']} · "
                  f"prompt {report['prompt_sha']} · pred-sim ≥ {report['pred_threshold']}")
    header = f"{'doc':<16}{'gold':>6}{'extr':>6}{'pair P/R/F1':>18}{'triple P/R/F1':>18}{'rev':>5}"
    console.print(f"[dim]{header}[/dim]")
    for name, d in report["docs"].items():
        console.print(
            f"{name:<16}{d['gold']:>6}{d['extracted']:>6}"
            f"{d['pair']['precision']:>7.2f}{d['pair']['recall']:>5.2f}{d['pair']['f1']:>6.2f}"
            f"{d['triple']['precision']:>7.2f}{d['triple']['recall']:>5.2f}{d['triple']['f1']:>6.2f}"
            f"{d['reversed_matches']:>5}")
    console.print(
        f"[bold]{'TOTAL':<16}{agg['gold']:>6}{agg['extracted']:>6}"
        f"{agg['pair']['precision']:>7.2f}{agg['pair']['recall']:>5.2f}{agg['pair']['f1']:>6.2f}"
        f"{agg['triple']['precision']:>7.2f}{agg['triple']['recall']:>5.2f}{agg['triple']['f1']:>6.2f}"
        f"{agg['reversed_matches']:>5}[/bold]")
    console.print(f"[dim]full report (misses + false positives): {report['report_path']}[/dim]")


@app.command()
def search(query: str, as_of: str = typer.Option(None, help="ISO date: facts valid at that time")):
    """Retrieve graph facts relevant to a query (anchors + neighborhood)."""
    from kgi.retrieval import retrieve

    result = retrieve(query, as_of=as_of)
    if not result["anchors"]:
        console.print("no matching entities in the graph")
        raise typer.Exit()
    console.print("anchors: " + ", ".join(
        f"{a['name']} ({a['score']:.2f})" for a in result["anchors"]))
    for f in sorted(result["facts"], key=lambda f: f.subject):
        srcs = ", ".join(s.split("/")[-1] for s in f.sources) or "n/a"
        console.print(f"  {f.render()}  [dim]support {f.support} · {srcs}[/dim]")


@app.command()
def ask(
    question: str,
    as_of: str = typer.Option(None, help="Answer as of this ISO date (time travel)"),
):
    """Ask a question; the answer is grounded in reviewed facts and cites them."""
    from kgi.retrieval import answer as _answer

    result = _answer(question, as_of=as_of)
    when = f" (as of {as_of})" if as_of else ""
    console.print(f"[bold]{result['answer']}[/bold]{when}")
    for c in result.get("citations", []):
        srcs = ", ".join(s.split("/")[-1] for s in c["sources"]) or "n/a"
        console.print(f"  • {c['fact']}  [dim]({srcs})[/dim]")


@app.command()
def serve(port: int = 8100, host: str = "127.0.0.1"):
    """Serve the graph viewer (canonical graph + provenance + review queue)."""
    import uvicorn

    from kgi.viewer import create_app

    console.print(f"kgi viewer on [bold]http://{host}:{port}[/bold]")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
