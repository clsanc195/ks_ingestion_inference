"""BPMN native XML parser (L0). BPMN is ground truth: it is already a typed graph,
so flow structure becomes ground_truth_triples that bypass LLM extraction."""

from pathlib import Path

from lxml import etree

from kgi.ingestion.base import register_parser
from kgi.models import Document, Modality, NormalizedDocument
from kgi.models.document import Block

_BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
_FLOW_NODE_TAGS = (
    "task", "userTask", "serviceTask", "scriptTask", "manualTask", "sendTask",
    "receiveTask", "businessRuleTask", "callActivity", "subProcess",
    "startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent",
    "boundaryEvent", "exclusiveGateway", "parallelGateway", "inclusiveGateway",
    "eventBasedGateway",
)


class BpmnParser:
    modality = Modality.bpmn

    def parse(self, doc: Document, path: Path) -> NormalizedDocument:
        root = etree.parse(str(path)).getroot()
        blocks: list[Block] = []
        triples: list[dict] = []
        names: dict[str, str] = {}

        for tag in _FLOW_NODE_TAGS:
            for el in root.iter(f"{{{_BPMN_NS}}}{tag}"):
                el_id = el.get("id") or ""
                name = el.get("name") or el_id
                names[el_id] = name
                blocks.append(
                    Block(
                        block_id=el_id,
                        kind=f"bpmn_{tag}",
                        text=name,
                        context={"bpmn_type": tag},
                    )
                )
                triples.append(
                    {"subject": name, "predicate": "IS_A", "object": tag, "block_id": el_id}
                )

        for flow in root.iter(f"{{{_BPMN_NS}}}sequenceFlow"):
            src, tgt = flow.get("sourceRef"), flow.get("targetRef")
            if src and tgt:
                triples.append(
                    {
                        "subject": names.get(src, src),
                        "predicate": "FLOWS_TO",
                        "object": names.get(tgt, tgt),
                        "block_id": flow.get("id") or "",
                        "condition": flow.get("name"),
                    }
                )

        return NormalizedDocument(doc=doc, blocks=blocks, ground_truth_triples=triples)


register_parser(BpmnParser())
