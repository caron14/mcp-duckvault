"""DuckDB persistence and traversal for the local Markdown graph."""

from __future__ import annotations

import json
from typing import Any, Iterable

import duckdb

from .db_manager import DatabaseManager
from .graph_extractor import GraphData, GraphExtractor, GraphNode


class GraphRepository:
    """Persist and query graph records through an existing database connection."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self.db.conn is None:
            self.db.connect()
        return self.db.conn

    def delete_document_graph(self, document_path: str) -> None:
        path = GraphExtractor.normalize_path(document_path)
        self.conn.execute("DELETE FROM edges WHERE document_path = ?", (path,))
        self.conn.execute("DELETE FROM node_mentions WHERE document_path = ?", (path,))
        self.conn.execute(
            """
            DELETE FROM nodes
            WHERE document_path = ?
              AND node_type IN ('document', 'okf_concept', 'heading', 'okf_index', 'okf_log')
            """,
            (path,),
        )

    def upsert_document_graph(self, document_path: str, graph_data: GraphData) -> None:
        path = GraphExtractor.normalize_path(document_path)
        for node in graph_data.nodes:
            # A document-like node is owned and written only by that document.
            if node.document_path not in {None, path}:
                continue
            self._upsert_node(node)
        for edge in graph_data.edges:
            self.conn.execute(
                """
                INSERT INTO edges (
                    edge_id, source_node_id, target_node_id, edge_type,
                    weight, document_path, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (edge_id) DO UPDATE SET
                    source_node_id = excluded.source_node_id,
                    target_node_id = excluded.target_node_id,
                    edge_type = excluded.edge_type,
                    weight = excluded.weight,
                    document_path = excluded.document_path,
                    metadata = excluded.metadata
                """,
                (
                    edge.edge_id,
                    edge.source_node_id,
                    edge.target_node_id,
                    edge.edge_type,
                    edge.weight,
                    edge.document_path,
                    self._dump(edge.metadata),
                ),
            )
        for mention in graph_data.mentions:
            self.conn.execute(
                """
                INSERT INTO node_mentions (
                    mention_id, node_id, chunk_id, document_path, mention_text, metadata
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (mention_id) DO UPDATE SET
                    node_id = excluded.node_id,
                    chunk_id = excluded.chunk_id,
                    document_path = excluded.document_path,
                    mention_text = excluded.mention_text,
                    metadata = excluded.metadata
                """,
                (
                    mention.mention_id,
                    mention.node_id,
                    mention.chunk_id,
                    mention.document_path,
                    mention.mention_text,
                    self._dump(mention.metadata),
                ),
            )

    def collect_garbage(self) -> None:
        self.conn.execute("""
            DELETE FROM nodes
            WHERE document_path IS NULL
              AND node_id NOT IN (
                  SELECT source_node_id FROM edges
                  UNION
                  SELECT target_node_id FROM edges
              )
            """)

    def find_neighbors(
        self, document_path: str, depth: int = 1, limit: int = 20
    ) -> list[dict[str, Any]]:
        path = GraphExtractor.normalize_path(document_path)
        return self._traverse([f"doc:{path}"], depth, limit)

    def expand_from_documents(
        self, document_paths: list[str], depth: int = 1, limit: int = 20
    ) -> list[dict[str, Any]]:
        seeds = [
            f"doc:{GraphExtractor.normalize_path(path)}" for path in dict.fromkeys(document_paths)
        ]
        return self._traverse(seeds, depth, limit)

    def find_okf_concept(self, concept_id: str) -> dict[str, Any] | None:
        concept = GraphExtractor.concept_id(concept_id)
        path = f"{concept}.md"
        row = self.conn.execute(
            """
            SELECT node_id, node_type, name, document_path, metadata
            FROM nodes
            WHERE node_type = 'okf_concept' AND document_path = ?
            """,
            (path,),
        ).fetchone()
        return self._describe_concept(self._node_dict(row)) if row else None

    def search_okf_concepts(
        self, okf_type: str | None = None, tag: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        rows = self.conn.execute("""
            SELECT node_id, node_type, name, document_path, metadata
            FROM nodes
            WHERE node_type = 'okf_concept'
            ORDER BY name, document_path
            """).fetchall()
        expected_type = okf_type.casefold() if okf_type else None
        expected_tag = tag.lstrip("#").casefold() if tag else None
        results = []
        for row in rows:
            concept = self._describe_concept(self._node_dict(row))
            if expected_type and str(concept.get("type", "")).casefold() != expected_type:
                continue
            if expected_tag and expected_tag not in {
                str(item).casefold() for item in concept.get("tags", [])
            }:
                continue
            results.append(concept)
            if len(results) >= max(0, limit):
                break
        return results

    def get_graph_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Return all persisted graph records in deterministic order."""
        node_rows = self.conn.execute("""
            SELECT node_id, node_type, name, document_path, metadata
            FROM nodes
            ORDER BY node_id
            """).fetchall()
        edge_rows = self.conn.execute("""
            SELECT edge_id, source_node_id, target_node_id, edge_type,
                   weight, document_path, metadata
            FROM edges
            ORDER BY edge_id
            """).fetchall()
        return {
            "nodes": [self._node_dict(row) for row in node_rows],
            "edges": [
                {
                    "edge_id": row[0],
                    "source_node_id": row[1],
                    "target_node_id": row[2],
                    "edge_type": row[3],
                    "weight": row[4],
                    "document_path": row[5],
                    "metadata": self._load(row[6]),
                }
                for row in edge_rows
            ],
        }

    def _traverse(self, seed_ids: list[str], depth: int, limit: int) -> list[dict[str, Any]]:
        if depth < 1 or limit < 1 or not seed_ids:
            return []
        visited = set(seed_ids)
        frontier = set(seed_ids)
        results: list[dict[str, Any]] = []

        for current_depth in range(1, depth + 1):
            if not frontier or len(results) >= limit:
                break
            placeholders = ", ".join("?" for _ in frontier)
            values = list(frontier)
            edges = self.conn.execute(
                f"""
                SELECT source_node_id, target_node_id, edge_type, weight, metadata
                FROM edges
                WHERE source_node_id IN ({placeholders})
                   OR target_node_id IN ({placeholders})
                ORDER BY edge_type, source_node_id, target_node_id
                """,
                values + values,
            ).fetchall()
            candidates: dict[str, tuple] = {}
            for edge in edges:
                source, target = edge[0], edge[1]
                if source in frontier:
                    neighbor, direction = target, "outgoing"
                elif target in frontier:
                    neighbor, direction = source, "incoming"
                else:
                    continue
                if neighbor not in visited:
                    candidates.setdefault(neighbor, (edge, direction))

            nodes = self._get_nodes(candidates)
            next_frontier = set()
            for node_id, (edge, direction) in candidates.items():
                if node_id not in nodes:
                    continue
                node = nodes[node_id]
                node.update(
                    {
                        "edge_type": edge[2],
                        "weight": edge[3],
                        "depth": current_depth,
                        "direction": direction,
                        "edge_metadata": self._load(edge[4]),
                        "graph_score": edge[3] / current_depth,
                    }
                )
                results.append(node)
                visited.add(node_id)
                next_frontier.add(node_id)
                if len(results) >= limit:
                    break
            frontier = next_frontier

        return results

    def _describe_concept(self, concept: dict[str, Any]) -> dict[str, Any]:
        related = self._traverse([concept["node_id"]], 1, 100)
        by_type: dict[str, list[dict[str, Any]]] = {}
        for item in related:
            by_type.setdefault(item["node_type"], []).append(item)
        metadata = concept.get("metadata", {})
        concept.update(
            {
                "concept_id": metadata.get(
                    "concept_id", GraphExtractor.concept_id(concept["document_path"])
                ),
                "title": concept["name"],
                "type": self._first_name(by_type.get("okf_type")),
                "description": metadata.get("description"),
                "timestamp": metadata.get("timestamp"),
                "resources": [item for item in by_type.get("resource", [])],
                "resource": self._first_name(by_type.get("resource")),
                "tags": [item["name"] for item in by_type.get("tag", [])],
                "headings": [item["name"] for item in by_type.get("heading", [])],
                "citations": [item for item in by_type.get("citation", [])],
                "linked_concepts": [
                    item
                    for item in related
                    if item["node_type"] == "okf_concept"
                    and item["edge_type"] in {"LINKS_TO", "MENTIONS_LINK"}
                ],
                "related_notes": [
                    item
                    for item in related
                    if item["node_type"]
                    in {
                        "document",
                        "okf_concept",
                        "okf_index",
                        "okf_log",
                    }
                ],
            }
        )
        return concept

    def _upsert_node(self, node: GraphNode) -> None:
        values = (
            node.node_id,
            node.node_type,
            node.name,
            node.document_path,
            self._dump(node.metadata),
        )
        self.conn.execute(
            """
            INSERT INTO nodes (node_id, node_type, name, document_path, metadata)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (node_id) DO UPDATE SET
                node_type = excluded.node_type,
                name = excluded.name,
                document_path = excluded.document_path,
                metadata = excluded.metadata,
                updated_at = now()
            """,
            values,
        )

    def _get_nodes(self, node_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = list(node_ids)
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""
            SELECT node_id, node_type, name, document_path, metadata
            FROM nodes WHERE node_id IN ({placeholders})
            """,
            ids,
        ).fetchall()
        return {row[0]: self._node_dict(row) for row in rows}

    @classmethod
    def _node_dict(cls, row: tuple) -> dict[str, Any]:
        return {
            "node_id": row[0],
            "node_type": row[1],
            "name": row[2],
            "document_path": row[3],
            "metadata": cls._load(row[4]),
        }

    @staticmethod
    def _first_name(items: list[dict[str, Any]] | None) -> str | None:
        return items[0]["name"] if items else None

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _load(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        return json.loads(value) if isinstance(value, str) else value
