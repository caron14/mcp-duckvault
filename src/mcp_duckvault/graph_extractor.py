"""Extract a local knowledge graph from Markdown and OKF documents."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    node_type: str
    name: str
    document_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    edge_type: str
    document_path: str
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeMention:
    mention_id: str
    node_id: str
    document_path: str
    mention_text: str
    chunk_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphData:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    mentions: list[NodeMention] = field(default_factory=list)
    _node_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _edge_ids: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        self._node_ids.update(node.node_id for node in self.nodes)
        self._edge_ids.update(edge.edge_id for edge in self.edges)

    def add_node(self, node: GraphNode) -> None:
        if node.node_id not in self._node_ids:
            self.nodes.append(node)
            self._node_ids.add(node.node_id)

    def add_edge(self, edge: GraphEdge) -> None:
        if edge.edge_id not in self._edge_ids:
            self.edges.append(edge)
            self._edge_ids.add(edge.edge_id)

    @property
    def node_mentions(self) -> list[NodeMention]:
        return self.mentions


class GraphExtractor:
    """Extract deterministic graph records from one Markdown document."""

    WIKI_LINK_RE = re.compile(r"(?<!!)\[\[([^\]\n]+)\]\]")
    MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]*)\]\(([^)\n]+)\)")
    HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$", re.MULTILINE)
    INLINE_TAG_RE = re.compile(r"(?<![\w#/])#([\w][\w/-]*)", re.UNICODE)

    def __init__(self, vault_path: str | None = None):
        self.vault_path = os.path.abspath(vault_path) if vault_path else None

    def extract(self, document_path: str, metadata: dict[str, Any] | None, body: str) -> GraphData:
        path = self.normalize_path(document_path)
        metadata = self._jsonable(metadata or {})
        data = GraphData()
        doc_id = f"doc:{path}"
        filename = posixpath.basename(path)
        reserved = filename.casefold()
        is_okf = "type" in metadata and reserved not in {"index.md", "log.md"}
        node_type = (
            "okf_index"
            if reserved == "index.md"
            else "okf_log" if reserved == "log.md" else "okf_concept" if is_okf else "document"
        )
        name = str(metadata.get("title") or posixpath.splitext(filename)[0])
        node_metadata = dict(metadata)
        if is_okf:
            node_metadata["concept_id"] = self.concept_id(path)
        data.add_node(GraphNode(doc_id, node_type, name, path, node_metadata))

        self._extract_folders(data, path, doc_id)
        searchable_body = self._without_fenced_code(body)
        self._extract_headings(data, path, doc_id, searchable_body)
        self._extract_tags(data, path, doc_id, metadata, searchable_body)
        self._extract_links(data, path, doc_id, node_type, searchable_body)

        if is_okf:
            self._extract_okf_fields(data, path, doc_id, metadata)

        return data

    @staticmethod
    def concept_id(document_path: str) -> str:
        path = GraphExtractor.normalize_path(document_path)
        return path[:-3] if path.casefold().endswith(".md") else path

    def _extract_folders(self, data: GraphData, path: str, doc_id: str) -> None:
        folder = posixpath.dirname(path)
        if not folder:
            return
        parts = folder.split("/")
        parent_id = None
        for index in range(len(parts)):
            folder_path = "/".join(parts[: index + 1])
            folder_id = f"folder:{folder_path}"
            data.add_node(GraphNode(folder_id, "folder", parts[index]))
            if parent_id:
                data.add_edge(self._edge(parent_id, folder_id, "CONTAINS", path))
            parent_id = folder_id
        data.add_edge(self._edge(parent_id, doc_id, "CONTAINS", path))

    def _extract_headings(self, data: GraphData, path: str, doc_id: str, body: str) -> None:
        slugs: dict[str, int] = {}
        for match in self.HEADING_RE.finditer(body):
            title = match.group(2).strip()
            base_slug = self._slug(title)
            count = slugs.get(base_slug, 0)
            slugs[base_slug] = count + 1
            slug = base_slug if count == 0 else f"{base_slug}-{count}"
            heading_id = f"heading:{path}#{slug}"
            level = len(match.group(1))
            data.add_node(
                GraphNode(heading_id, "heading", title, path, {"level": level, "slug": slug})
            )
            data.add_edge(
                self._edge(doc_id, heading_id, "HAS_HEADING", path, metadata={"level": level})
            )

    def _extract_tags(
        self,
        data: GraphData,
        path: str,
        doc_id: str,
        metadata: dict[str, Any],
        body: str,
    ) -> None:
        frontmatter_tags = self._as_tags(metadata.get("tags")) | self._as_tags(metadata.get("tag"))
        inline_tags = {match.group(1).casefold() for match in self.INLINE_TAG_RE.finditer(body)}
        for tag in sorted(frontmatter_tags | inline_tags):
            tag_id = f"tag:{tag}"
            data.add_node(GraphNode(tag_id, "tag", tag))
            data.add_edge(self._edge(doc_id, tag_id, "HAS_TAG", path))
            data.mentions.append(
                self._mention(
                    tag_id,
                    path,
                    f"#{tag}",
                    {"source": "frontmatter" if tag in frontmatter_tags else "inline"},
                )
            )

    def _extract_links(
        self, data: GraphData, path: str, doc_id: str, node_type: str, body: str
    ) -> None:
        for match in self.WIKI_LINK_RE.finditer(body):
            raw = match.group(1).strip()
            target, separator, alias = raw.partition("|")
            target = target.strip()
            if not target:
                continue
            target_id, target_node = self._link_target(path, target)
            data.add_node(target_node)
            edge_type = self._link_edge_type(node_type, target, bool(separator))
            data.add_edge(
                self._edge(
                    doc_id,
                    target_id,
                    edge_type,
                    path,
                    metadata={"target": target, "alias": alias.strip() or None},
                )
            )
            data.mentions.append(
                self._mention(target_id, path, match.group(0), {"kind": "wiki_link"})
            )

        for match in self.MARKDOWN_LINK_RE.finditer(body):
            label, raw_target = match.groups()
            raw_target = raw_target.strip()
            target = (
                raw_target[1 : raw_target.find(">")]
                if raw_target.startswith("<") and ">" in raw_target
                else raw_target.split(maxsplit=1)[0]
            )
            if not target:
                continue
            if self._is_external(target):
                citation_id = f"citation:{target}"
                data.add_node(
                    GraphNode(
                        citation_id, "citation", label.strip() or target, metadata={"url": target}
                    )
                )
                data.add_edge(
                    self._edge(
                        doc_id,
                        citation_id,
                        "CITES_SOURCE",
                        path,
                        metadata={"label": label.strip()},
                    )
                )
                data.mentions.append(
                    self._mention(citation_id, path, match.group(0), {"kind": "external_link"})
                )
                continue
            target_id, target_node = self._link_target(path, target)
            data.add_node(target_node)
            data.add_edge(
                self._edge(
                    doc_id,
                    target_id,
                    self._link_edge_type(node_type, target),
                    path,
                    metadata={"target": target, "label": label.strip()},
                )
            )
            data.mentions.append(
                self._mention(target_id, path, match.group(0), {"kind": "markdown_link"})
            )

    def _extract_okf_fields(
        self, data: GraphData, path: str, doc_id: str, metadata: dict[str, Any]
    ) -> None:
        type_name = str(metadata["type"]).strip()
        if type_name:
            type_id = f"okf_type:{type_name.casefold()}"
            data.add_node(GraphNode(type_id, "okf_type", type_name))
            data.add_edge(self._edge(doc_id, type_id, "HAS_TYPE", path))

        resources = metadata.get("resource")
        if resources is None:
            return
        if not isinstance(resources, list):
            resources = [resources]
        for resource in resources:
            resource_metadata = resource if isinstance(resource, dict) else {"value": resource}
            resource_name = (
                resource_metadata.get("name")
                or resource_metadata.get("id")
                or resource_metadata.get("url")
                or resource
            )
            identity = (
                resource_metadata.get("id")
                or resource_metadata.get("url")
                or resource_metadata.get("name")
                or json.dumps(resource, ensure_ascii=False, sort_keys=True, default=str)
            )
            resource_id = f"resource:{identity}"
            data.add_node(
                GraphNode(
                    resource_id,
                    "resource",
                    str(resource_name),
                    metadata=self._jsonable(resource_metadata),
                )
            )
            data.add_edge(self._edge(doc_id, resource_id, "DESCRIBES_RESOURCE", path))

    def _link_target(self, source_path: str, raw_target: str) -> tuple[str, GraphNode]:
        target = urllib.parse.unquote(raw_target).split("#", 1)[0].split("?", 1)[0]
        if target.startswith("/"):
            resolved = target.lstrip("/")
        else:
            resolved = posixpath.join(posixpath.dirname(source_path), target)
        resolved = self.normalize_path(resolved)
        if not posixpath.splitext(resolved)[1]:
            resolved += ".md"
        exists = resolved == source_path
        if self.vault_path:
            exists = exists or os.path.isfile(os.path.join(self.vault_path, *resolved.split("/")))
        node_type = "document" if exists else "link_target"
        node_id = f"doc:{resolved}" if exists else f"link_target:{resolved}"
        name = posixpath.splitext(posixpath.basename(resolved))[0]
        document_path = resolved if exists else None
        return node_id, GraphNode(node_id, node_type, name, document_path)

    @staticmethod
    def _edge(
        source: str,
        target: str,
        edge_type: str,
        document_path: str,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> GraphEdge:
        key = "\0".join((document_path, source, edge_type, target))
        edge_id = f"edge:{hashlib.sha1(key.encode()).hexdigest()}"
        return GraphEdge(edge_id, source, target, edge_type, document_path, weight, metadata or {})

    @staticmethod
    def _mention(
        node_id: str, document_path: str, text: str, metadata: dict[str, Any]
    ) -> NodeMention:
        key = "\0".join((document_path, node_id, text, json.dumps(metadata, sort_keys=True)))
        mention_id = f"mention:{hashlib.sha1(key.encode()).hexdigest()}"
        return NodeMention(mention_id, node_id, document_path, text, metadata=metadata)

    @staticmethod
    def normalize_path(path: str) -> str:
        normalized = posixpath.normpath(path.replace("\\", "/")).lstrip("/")
        return "" if normalized == "." else normalized

    @staticmethod
    def _without_fenced_code(body: str) -> str:
        return re.sub(r"^(```|~~~).*?^\1\s*$", "", body, flags=re.MULTILINE | re.DOTALL)

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^\w\s-]", "", value.casefold(), flags=re.UNICODE)
        return re.sub(r"[-\s]+", "-", slug).strip("-") or "heading"

    @staticmethod
    def _as_tags(value: Any) -> set[str]:
        if value is None:
            return set()
        values = value if isinstance(value, list) else re.split(r"[\s,]+", str(value))
        return {
            str(tag).strip().lstrip("#").casefold()
            for tag in values
            if str(tag).strip().lstrip("#")
        }

    @staticmethod
    def _is_external(target: str) -> bool:
        return bool(urllib.parse.urlparse(target).scheme) or target.startswith("//")

    @staticmethod
    def _link_edge_type(node_type: str, target: str, has_alias: bool = False) -> str:
        if node_type == "okf_index":
            return (
                "HAS_LOG" if posixpath.basename(target).casefold() == "log.md" else "LISTS_CONCEPT"
            )
        return "MENTIONS_LINK" if has_alias else "LINKS_TO"

    @staticmethod
    def _jsonable(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
