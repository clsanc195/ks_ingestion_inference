"""Schema / ontology manager (L3).

Open-domain decision (§7): thin upper-level seed + dynamic induction. Extraction may
propose types outside the current schema; a proposed type only becomes *approved*
(and thereafter constrains extraction guidance) through the same HITL gate as facts.
New-type candidates are always routed to review regardless of confidence (L8).
"""

from pydantic import BaseModel, Field

# Thin upper ontology — the only hand-authored seed in an open-domain deployment.
UPPER_ONTOLOGY: dict[str, str] = {
    "Entity": "Anything that exists and can be referred to",
    "Agent": "A person, organization, or system that can act",
    "Event": "Something that happens at a time",
    "Artifact": "A produced thing: document, product, system, dataset",
    "Concept": "An abstract idea, category, or process definition",
    "Place": "A physical or logical location",
}


class TypeProposal(BaseModel):
    type_name: str
    parent: str  # must be an approved type (upper ontology at minimum)
    description: str
    example_entities: list[str] = Field(default_factory=list)
    proposed_by_run: str


class SchemaManager:
    """Tracks approved + proposed types/predicates. v1 keeps state in the graph store;
    this class is the single authority the extractor and validator consult."""

    def __init__(self) -> None:
        self._approved_types: dict[str, str] = dict(UPPER_ONTOLOGY)
        self._approved_predicates: set[str] = set()
        self._proposals: dict[str, TypeProposal] = {}

    @property
    def known_types(self) -> list[str]:
        return sorted(self._approved_types)

    @property
    def known_predicates(self) -> list[str]:
        return sorted(self._approved_predicates)

    def is_new_type(self, entity_type: str) -> bool:
        return entity_type not in self._approved_types

    def propose_type(self, proposal: TypeProposal) -> None:
        self._proposals.setdefault(proposal.type_name, proposal)

    def approve_type(self, type_name: str) -> None:
        p = self._proposals.pop(type_name, None)
        self._approved_types[type_name] = p.description if p else ""

    def curation_candidates(self) -> list[tuple[str, str]]:
        """Near-duplicate approved types for the periodic curation pass (schema-drift
        containment, §6). v1 stub: implement with embedding similarity over type
        names + descriptions."""
        raise NotImplementedError
