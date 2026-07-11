"""Temporal-filter semantics of neighborhood retrieval, against a live Neo4j.

Seeds a tiny bi-temporal graph directly (no LLM, no vectors):
  (Org)-[hq]->(OldCity)   valid_from 2019,    valid_to 2026-03  (superseded)
  (Org)-[hq]->(NewCity)   valid_from 2026-03, open
  (Org)-[founded_by]->(Founder)  no dates, open
"""

import pytest

from kgi.retrieval.search import neighborhood_facts
from kgi.stores.neo4j import CanonicalGraph


def _connect() -> CanonicalGraph | None:
    try:
        g = CanonicalGraph()
        g._driver.verify_connectivity()
        return g
    except Exception:
        return None


import os

_graph = _connect()
pytestmark = pytest.mark.skipif(
    _graph is None or os.environ.get("KGI_TEST_ALLOW_WIPE") != "1",
    reason="Neo4j not reachable, or KGI_TEST_ALLOW_WIPE=1 not set — "
           "these tests WIPE the database they point at",
)


@pytest.fixture()
def graph():
    with _graph.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
        s.run(
            "CREATE (o:Canonical {id:'org', name:'Org', entity_type:'Agent'}), "
            "(a:Canonical {id:'old', name:'OldCity', entity_type:'Place'}), "
            "(b:Canonical {id:'new', name:'NewCity', entity_type:'Place'}), "
            "(f:Canonical {id:'fdr', name:'Founder', entity_type:'Agent'}), "
            "(o)-[:REL {id:'e_old', predicate:'hq', valid_from:'2019', "
            "           valid_to:'2026-03', support:1}]->(a), "
            "(o)-[:REL {id:'e_new', predicate:'hq', valid_from:'2026-03', support:1}]->(b), "
            "(o)-[:REL {id:'e_f', predicate:'founded_by', support:1}]->(f)"
        )
    return _graph


def _objects(facts):
    return {f.object for f in facts}


def test_current_view_excludes_superseded(graph):
    facts = neighborhood_facts(graph, ["org"], hops=1)
    assert _objects(facts) == {"NewCity", "Founder"}


def test_as_of_past_returns_old_fact(graph):
    facts = neighborhood_facts(graph, ["org"], hops=1, as_of="2024-06-01")
    assert _objects(facts) == {"OldCity", "Founder"}  # NewCity not yet valid


def test_as_of_after_move_returns_new_fact(graph):
    facts = neighborhood_facts(graph, ["org"], hops=1, as_of="2026-07-01")
    assert _objects(facts) == {"NewCity", "Founder"}


def test_as_of_before_everything(graph):
    facts = neighborhood_facts(graph, ["org"], hops=1, as_of="2018-01-01")
    # hq(old) starts 2019 -> excluded; undated founded_by has open ends -> included
    assert _objects(facts) == {"Founder"}


def test_hop_expansion_and_limit(graph):
    with graph.session() as s:
        s.run(
            "MATCH (b:Canonical {id:'new'}) "
            "CREATE (c:Canonical {id:'cty', name:'Country', entity_type:'Place'}), "
            "(b)-[:REL {id:'e_in', predicate:'located_in', support:1}]->(c)"
        )
    one_hop = neighborhood_facts(graph, ["org"], hops=1)
    two_hop = neighborhood_facts(graph, ["org"], hops=2)
    assert "Country" not in _objects(one_hop)
    assert "Country" in _objects(two_hop)
    capped = neighborhood_facts(graph, ["org"], hops=2, limit=1)
    assert len(capped) == 1
