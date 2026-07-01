"""Generate offline HTML and JSON views of the persisted knowledge graph."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import urllib.parse
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

from .graph_repository import GraphRepository

logger = logging.getLogger(__name__)

DOCUMENT_TYPES = {"document", "okf_concept", "okf_index", "okf_log"}
CONCEPT_TYPES = {"document", "okf_concept"}
DOCUMENT_EDGE_TYPES = {"LINKS_TO", "MENTIONS_LINK", "LISTS_CONCEPT", "HAS_LOG"}
STRUCTURAL_GROUPS = {
    "folder": "folder",
    "heading": "heading",
    "tag": "tag",
    "okf_type": "okf_type",
    "resource": "resource",
    "citation": "citation",
    "link_target": "dangling",
}
GROUP_COLORS = {
    "document": "#4f46e5",
    "folder": "#f59e0b",
    "heading": "#64748b",
    "tag": "#10b981",
    "okf_type": "#8b5cf6",
    "resource": "#0ea5e9",
    "citation": "#ec4899",
    "dangling": "#ef4444",
    "other": "#94a3b8",
}


def build_graph_snapshot(
    repository: GraphRepository,
    vault_path: str | Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the versioned JSON model consumed by the offline viewer."""
    raw = repository.get_graph_snapshot()
    vault = Path(vault_path).resolve()
    node_ids = {node["node_id"] for node in raw["nodes"]}
    node_types = {node["node_id"]: node["node_type"] for node in raw["nodes"]}
    raw_edges = raw["edges"]

    document_link_edges = [
        edge
        for edge in raw_edges
        if edge["edge_type"] in DOCUMENT_EDGE_TYPES
        and edge["source_node_id"] in node_ids
        and edge["target_node_id"] in node_ids
        and node_types[edge["source_node_id"]] in DOCUMENT_TYPES
        and node_types[edge["target_node_id"]] in DOCUMENT_TYPES
    ]
    in_degree = {node_id: 0 for node_id in node_ids}
    out_degree = {node_id: 0 for node_id in node_ids}
    for edge in document_link_edges:
        out_degree[edge["source_node_id"]] += 1
        in_degree[edge["target_node_id"]] += 1

    nodes = []
    for node in raw["nodes"]:
        metadata = node["metadata"] if isinstance(node["metadata"], dict) else {}
        node_type = node["node_type"]
        path = node["document_path"]
        tags = _tags(metadata)
        display_type = str(metadata.get("type") or node_type)
        group = (
            "document" if node_type in DOCUMENT_TYPES else STRUCTURAL_GROUPS.get(node_type, "other")
        )
        degree = in_degree[node["node_id"]] + out_degree[node["node_id"]]
        resource_url = _resource_url(metadata.get("resource"))
        nodes.append(
            {
                "id": node["node_id"],
                "node_type": node_type,
                "group": group,
                "label": node["name"],
                "path": path,
                "concept_id": metadata.get("concept_id"),
                "display_type": display_type,
                "tags": tags,
                "directory": _directory(path),
                "resource_url": resource_url,
                "obsidian_uri": _obsidian_uri(vault.name, path) if path else None,
                "metadata": metadata,
                "in_degree": in_degree[node["node_id"]],
                "out_degree": out_degree[node["node_id"]],
                "degree": degree,
                "size": 25 + min(35, degree * 5),
                "color": (
                    _type_color(display_type)
                    if group == "document"
                    else GROUP_COLORS.get(group, GROUP_COLORS["other"])
                ),
                "initial_visible": group == "document",
                "search_text": " ".join(
                    filter(
                        None,
                        [
                            node["name"],
                            node["node_id"],
                            path,
                            metadata.get("concept_id"),
                            " ".join(tags),
                        ],
                    )
                ).casefold(),
            }
        )

    node_by_id = {node["id"]: node for node in nodes}
    edges = []
    for edge in raw_edges:
        source = edge["source_node_id"]
        target = edge["target_node_id"]
        renderable = source in node_by_id and target in node_by_id
        initial_visible = (
            renderable
            and edge["edge_type"] in DOCUMENT_EDGE_TYPES
            and node_by_id[source]["group"] == "document"
            and node_by_id[target]["group"] == "document"
        )
        edges.append(
            {
                "id": edge["edge_id"],
                "source": source,
                "target": target,
                "relation": edge["edge_type"],
                "weight": edge["weight"],
                "document_path": edge["document_path"],
                "metadata": edge["metadata"],
                "renderable": renderable,
                "initial_visible": initial_visible,
                "color": _type_color(edge["edge_type"]),
            }
        )

    diagnostics = _diagnostics(nodes, edges)
    return {
        "schema_version": "1.0",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "vault": {"name": vault.name},
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "documents": sum(node["group"] == "document" for node in nodes),
            "dangling_links": len(diagnostics["dangling_links"]),
        },
        "nodes": nodes,
        "edges": edges,
        "diagnostics": diagnostics,
    }


def render_graph_html(snapshot: dict[str, Any]) -> str:
    """Render a self-contained HTML document without external network dependencies."""
    graph_json = _safe_json(snapshot)
    cytoscape = (
        files("mcp_duckvault")
        .joinpath("assets/cytoscape-3.34.0.min.js")
        .read_text(encoding="utf-8")
        .replace("</script", r"<\/script")
    )
    return (
        _HTML_TEMPLATE.replace("__GRAPH_DATA__", graph_json)
        .replace("__CYTOSCAPE_JS__", cytoscape)
        .replace("__CYTOSCAPE_VERSION__", "3.34.0")
    )


def write_graph_visualization(
    repository: GraphRepository,
    vault_path: str | Path,
    html_path: str | Path,
    json_path: str | Path | None = None,
) -> tuple[Path, Path]:
    """Atomically write the HTML viewer and its canonical JSON sidecar."""
    html_output = Path(html_path).expanduser().resolve()
    json_output = (
        Path(json_path).expanduser().resolve() if json_path else html_output.with_suffix(".json")
    )
    if html_output == json_output:
        raise ValueError("HTML and JSON output paths must be different")

    snapshot = build_graph_snapshot(repository, vault_path)
    for warning in snapshot["diagnostics"]["warnings"]:
        logger.warning(warning)
    graph_json = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    html = render_graph_html(snapshot)
    _atomic_write(json_output, graph_json)
    _atomic_write(html_output, html)
    return html_output, json_output


def _diagnostics(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    document_nodes = [node for node in nodes if node["node_type"] in CONCEPT_TYPES]
    orphan_nodes = [_node_summary(node) for node in document_nodes if node["degree"] == 0]

    incoming: dict[str, list[str]] = {}
    for edge in edges:
        incoming.setdefault(edge["target"], []).append(edge["source"])
    dangling = [
        {
            **_node_summary(node),
            "referenced_by": sorted(incoming.get(node["id"], [])),
        }
        for node in nodes
        if node["node_type"] == "link_target"
    ]
    dangling.extend(
        {
            "id": edge["id"],
            "label": edge["relation"],
            "path": edge["document_path"],
            "referenced_by": [edge["source"]],
            "missing_endpoint": edge["target"],
        }
        for edge in edges
        if not edge["renderable"]
    )

    titles: dict[str, list[dict[str, Any]]] = {}
    for node in document_nodes:
        titles.setdefault(node["label"].casefold(), []).append(_node_summary(node))
    duplicate_titles = [
        {"title": items[0]["label"], "nodes": items} for items in titles.values() if len(items) > 1
    ]

    high_degree = [
        _node_summary(node) | {"degree": node["degree"]}
        for node in sorted(document_nodes, key=lambda item: (-item["degree"], item["id"]))
        if node["degree"] > 0
    ][:10]
    warnings = []
    if len(nodes) > 5000:
        warnings.append(
            f"Large graph: {len(nodes)} nodes. Keep structural node groups hidden when possible."
        )
    return {
        "orphan_nodes": orphan_nodes,
        "dangling_links": dangling,
        "duplicate_titles": duplicate_titles,
        "high_degree_nodes": high_degree,
        "warnings": warnings,
    }


def _node_summary(node: dict[str, Any]) -> dict[str, Any]:
    return {"id": node["id"], "label": node["label"], "path": node["path"]}


def _tags(metadata: dict[str, Any]) -> list[str]:
    values = []
    for key in ("tags", "tag"):
        value = metadata.get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value is not None:
            values.extend(re.split(r"[\s,]+", str(value)))
    return sorted(
        {
            str(value).strip().lstrip("#").casefold()
            for value in values
            if str(value).strip().lstrip("#")
        }
    )


def _resource_url(resource: Any) -> str | None:
    values = resource if isinstance(resource, list) else [resource]
    for value in values:
        candidate = value.get("url") if isinstance(value, dict) else value
        if isinstance(candidate, str) and urllib.parse.urlparse(candidate).scheme in {
            "http",
            "https",
        }:
            return candidate
    return None


def _directory(path: str | None) -> str:
    return str(Path(path).parent).replace("\\", "/") if path and "/" in path else ""


def _obsidian_uri(vault_name: str, path: str) -> str:
    return (
        f"obsidian://open?vault={urllib.parse.quote(vault_name)}"
        f"&file={urllib.parse.quote(path)}"
    )


def _type_color(value: str) -> str:
    hue = int(hashlib.sha1(value.casefold().encode()).hexdigest()[:6], 16) % 360
    return f"hsl({hue}, 65%, 52%)"


def _safe_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        .replace("&", r"\u0026")
        .replace("<", r"\u003c")
        .replace(">", r"\u003e")
        .replace("\u2028", r"\u2028")
        .replace("\u2029", r"\u2029")
    )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'">
  <title>DuckVault Graph</title>
  <style>
    :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; height: 100vh; overflow: hidden; background: #f8fafc; color: #0f172a; }
    header { height: 58px; display: flex; align-items: center; gap: 18px; padding: 0 18px;
      background: #0f172a; color: white; }
    header h1 { font-size: 17px; margin: 0; }
    #summary { color: #cbd5e1; font-size: 13px; }
    main { display: grid; grid-template-columns: 330px 1fr; height: calc(100vh - 58px); }
    aside { overflow-y: auto; padding: 16px; border-right: 1px solid #e2e8f0; background: white; }
    #graph { min-width: 0; min-height: 0; }
    section { margin-bottom: 18px; }
    h2 { margin: 0 0 8px; font-size: 12px; color: #475569; text-transform: uppercase;
      letter-spacing: .08em; }
    input, select { width: 100%; border: 1px solid #cbd5e1; border-radius: 7px; padding: 8px;
      background: white; color: #0f172a; margin-bottom: 7px; }
    label.check { display: flex; gap: 7px; align-items: center; font-size: 13px; margin: 6px 0; }
    label.check input { width: auto; margin: 0; }
    .chips { display: flex; flex-wrap: wrap; gap: 5px; }
    .chip { border: 0; border-radius: 999px; padding: 5px 9px; font-size: 11px; cursor: pointer;
      background: #e2e8f0; color: #334155; }
    .chip.warn { background: #fee2e2; color: #991b1b; }
    #details { font-size: 12px; line-height: 1.45; overflow-wrap: anywhere; }
    #details .title { font-size: 15px; font-weight: 700; margin-bottom: 8px; }
    #details .row { padding: 4px 0; border-bottom: 1px solid #f1f5f9; }
    #details .key { color: #64748b; display: block; font-size: 10px; text-transform: uppercase; }
    #details a { color: #2563eb; }
    #relations { max-height: 180px; overflow-y: auto; }
    .muted { color: #64748b; font-size: 12px; }
    .warning { color: #991b1b; background: #fef2f2; border-radius: 6px; padding: 7px; margin: 5px 0; }
    @media (max-width: 760px) {
      main { grid-template-columns: 260px 1fr; }
      header { gap: 8px; padding: 0 10px; }
    }
  </style>
</head>
<body>
  <header>
    <h1 id="title">DuckVault Graph</h1>
    <div id="summary"></div>
  </header>
  <main>
    <aside>
      <section>
        <h2>Find and filter</h2>
        <input id="search" type="search" placeholder="Title, path, concept ID, tag">
        <select id="type-filter"><option value="">All types</option></select>
        <select id="tag-filter"><option value="">All tags</option></select>
        <select id="directory-filter"><option value="">All directories</option></select>
      </section>
      <section>
        <h2>Structural nodes</h2>
        <div id="group-toggles"></div>
      </section>
      <section>
        <h2>Relations</h2>
        <div id="relation-toggles"></div>
      </section>
      <section>
        <h2>Layout</h2>
        <select id="layout">
          <option value="cose">Force-directed</option>
          <option value="concentric">Concentric</option>
          <option value="breadthfirst">Breadth-first</option>
          <option value="circle">Circle</option>
          <option value="grid">Grid</option>
        </select>
      </section>
      <section>
        <h2>Quality</h2>
        <div id="warnings"></div>
        <div id="diagnostics" class="chips"></div>
      </section>
      <section>
        <h2>Selected node</h2>
        <div id="details" class="muted">Select a node to inspect its metadata and relations.</div>
      </section>
    </aside>
    <div id="graph" role="img" aria-label="Interactive knowledge graph"></div>
  </main>
  <script id="graph-data" type="application/json">__GRAPH_DATA__</script>
  <script>__CYTOSCAPE_JS__</script>
  <script>
  (() => {
    "use strict";
    const graph = JSON.parse(document.getElementById("graph-data").textContent);
    const nodeMap = new Map(graph.nodes.map(node => [node.id, node]));
    const renderableEdges = graph.edges.filter(edge => edge.renderable);
    const elements = [
      ...graph.nodes.map(node => ({
        data: node, style: { display: node.initial_visible ? "element" : "none" }
      })),
      ...renderableEdges.map(edge => ({
        data: edge, style: { display: edge.initial_visible ? "element" : "none" }
      }))
    ];
    document.getElementById("title").textContent = `${graph.vault.name} — Knowledge Graph`;
    document.getElementById("summary").textContent =
      `${graph.stats.documents} documents · ${graph.stats.nodes} nodes · ${graph.stats.edges} edges`;

    const cy = cytoscape({
      container: document.getElementById("graph"),
      elements,
      minZoom: 0.08,
      maxZoom: 4,
      wheelSensitivity: 0.2,
      style: [
        { selector: "node", style: {
          "background-color": "data(color)", "label": "data(label)", "width": "data(size)",
          "height": "data(size)", "font-size": 10, "text-wrap": "wrap", "text-max-width": 110,
          "text-valign": "bottom", "text-margin-y": 7, "border-width": 1,
          "border-color": "#ffffff", "overlay-opacity": 0
        }},
        { selector: 'node[group = "folder"]', style: { "shape": "round-rectangle" }},
        { selector: 'node[group = "tag"]', style: { "shape": "diamond" }},
        { selector: 'node[group = "dangling"]', style: {
          "shape": "vee", "border-width": 3, "border-color": "#991b1b"
        }},
        { selector: "node:selected", style: {
          "border-width": 4, "border-color": "#0f172a", "z-index": 999
        }},
        { selector: "edge", style: {
          "width": 1.5, "line-color": "data(color)", "target-arrow-color": "data(color)",
          "target-arrow-shape": "triangle", "curve-style": "bezier", "opacity": 0.62,
          "arrow-scale": 0.8
        }},
        { selector: "edge:selected", style: { "width": 4, "opacity": 1 }}
      ],
      layout: { name: "preset", fit: true, padding: 35 }
    });

    const groupLabels = {
      folder: "Folders", heading: "Headings", tag: "Tags", okf_type: "OKF types",
      resource: "Resources", citation: "Citations", dangling: "Dangling targets", other: "Other"
    };
    const structuralGroups = [...new Set(graph.nodes
      .filter(node => node.group !== "document").map(node => node.group))].sort();
    const relationTypes = [...new Set(renderableEdges.map(edge => edge.relation))].sort();
    const groupRoot = document.getElementById("group-toggles");
    structuralGroups.forEach(group => groupRoot.appendChild(check(group, groupLabels[group] || group, false)));
    const relationRoot = document.getElementById("relation-toggles");
    relationTypes.forEach(relation => relationRoot.appendChild(check(relation, relation, true, "relation")));

    fillSelect("type-filter", graph.nodes.filter(node => node.group === "document")
      .map(node => node.display_type));
    fillSelect("tag-filter", graph.nodes.flatMap(node => node.tags));
    fillSelect("directory-filter", graph.nodes.map(node => node.directory).filter(Boolean));

    function check(value, label, checked, kind = "group") {
      const wrapper = document.createElement("label");
      wrapper.className = "check";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = checked;
      input.dataset[kind] = value;
      input.addEventListener("change", () => applyFilters(true));
      const text = document.createElement("span");
      text.textContent = label;
      wrapper.append(input, text);
      return wrapper;
    }

    function fillSelect(id, values) {
      const select = document.getElementById(id);
      [...new Set(values)].sort((a, b) => a.localeCompare(b)).forEach(value => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        select.appendChild(option);
      });
    }

    function applyFilters(relayout = false) {
      const query = document.getElementById("search").value.trim().toLocaleLowerCase();
      const type = document.getElementById("type-filter").value;
      const tag = document.getElementById("tag-filter").value;
      const directory = document.getElementById("directory-filter").value;
      const groups = new Set([...document.querySelectorAll("[data-group]:checked")]
        .map(input => input.dataset.group));
      const relations = new Set([...document.querySelectorAll("[data-relation]:checked")]
        .map(input => input.dataset.relation));
      const matchingDocuments = new Set(graph.nodes.filter(node =>
        node.group === "document" &&
        (!query || node.search_text.includes(query)) &&
        (!type || node.display_type === type) &&
        (!tag || node.tags.includes(tag)) &&
        (!directory || node.directory === directory)
      ).map(node => node.id));
      const activeDocumentFilter = Boolean(query || type || tag || directory);
      const adjacentToMatch = new Set();
      renderableEdges.forEach(edge => {
        if (matchingDocuments.has(edge.source)) adjacentToMatch.add(edge.target);
        if (matchingDocuments.has(edge.target)) adjacentToMatch.add(edge.source);
      });

      cy.batch(() => {
        cy.nodes().forEach(element => {
          const node = element.data();
          const visible = node.group === "document"
            ? matchingDocuments.has(node.id)
            : groups.has(node.group) && (
                !activeDocumentFilter || adjacentToMatch.has(node.id) ||
                (query && node.search_text.includes(query))
              );
          element.style("display", visible ? "element" : "none");
        });
        cy.edges().forEach(element => {
          const edge = element.data();
          const visible = relations.has(edge.relation) &&
            element.source().style("display") !== "none" &&
            element.target().style("display") !== "none";
          element.style("display", visible ? "element" : "none");
        });
      });
      if (relayout) runLayout();
    }

    function runLayout() {
      const name = document.getElementById("layout").value;
      cy.elements(":visible").layout({
        name, animate: false, fit: true, padding: 35,
        directed: name === "breadthfirst", spacingFactor: 1.25
      }).run();
    }

    function showDetails(node) {
      const root = document.getElementById("details");
      root.replaceChildren();
      const title = document.createElement("div");
      title.className = "title";
      title.textContent = node.label;
      root.appendChild(title);
      row(root, "Node type", node.node_type);
      row(root, "OKF type", node.display_type);
      if (node.path) row(root, "Path", node.path);
      if (node.tags.length) row(root, "Tags", node.tags.join(", "));
      row(root, "Degree", `${node.in_degree} in / ${node.out_degree} out`);
      Object.entries(node.metadata || {}).forEach(([key, value]) => {
        if (!["type", "title", "tags", "tag"].includes(key)) {
          row(root, key, typeof value === "string" ? value : JSON.stringify(value));
        }
      });
      if (node.obsidian_uri) linkRow(root, "Open", "Open in Obsidian", node.obsidian_uri);
      if (node.resource_url) linkRow(root, "Resource", node.resource_url, node.resource_url);
      const relations = renderableEdges.filter(edge => edge.source === node.id || edge.target === node.id);
      if (relations.length) {
        const list = document.createElement("div");
        list.id = "relations";
        relations.forEach(edge => {
          const outgoing = edge.source === node.id;
          const other = nodeMap.get(outgoing ? edge.target : edge.source);
          row(list, outgoing ? `→ ${edge.relation}` : `← ${edge.relation}`,
            other ? other.label : (outgoing ? edge.target : edge.source));
        });
        root.appendChild(list);
      }
    }

    function row(root, key, value) {
      const item = document.createElement("div");
      item.className = "row";
      const label = document.createElement("span");
      label.className = "key";
      label.textContent = key;
      const content = document.createElement("span");
      content.textContent = value ?? "—";
      item.append(label, content);
      root.appendChild(item);
    }

    function linkRow(root, key, text, href) {
      const item = document.createElement("div");
      item.className = "row";
      const label = document.createElement("span");
      label.className = "key";
      label.textContent = key;
      const link = document.createElement("a");
      link.textContent = text;
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.append(label, link);
      root.appendChild(item);
    }

    function diagnosticChip(label, items, warning = false) {
      const button = document.createElement("button");
      button.className = warning && items.length ? "chip warn" : "chip";
      button.textContent = `${label}: ${items.length}`;
      button.title = items.map(item => item.path || item.label || item.title).filter(Boolean).join("\n");
      button.addEventListener("click", () => {
        const first = items[0]?.id || items[0]?.nodes?.[0]?.id;
        if (first && nodeMap.has(first)) {
          const node = nodeMap.get(first);
          if (node.group !== "document") {
            const toggle = document.querySelector(`[data-group="${node.group}"]`);
            if (toggle) toggle.checked = true;
            applyFilters(false);
          }
          showDetails(node);
          const element = cy.getElementById(first);
          element.select();
          cy.center(element);
        }
      });
      return button;
    }

    const diagnostics = document.getElementById("diagnostics");
    diagnostics.append(
      diagnosticChip("Orphans", graph.diagnostics.orphan_nodes, true),
      diagnosticChip("Dangling", graph.diagnostics.dangling_links, true),
      diagnosticChip("Duplicate titles", graph.diagnostics.duplicate_titles, true),
      diagnosticChip("High degree", graph.diagnostics.high_degree_nodes)
    );
    const warnings = document.getElementById("warnings");
    graph.diagnostics.warnings.forEach(message => {
      const item = document.createElement("div");
      item.className = "warning";
      item.textContent = message;
      warnings.appendChild(item);
    });

    cy.on("tap", "node", event => showDetails(event.target.data()));
    document.getElementById("search").addEventListener("input", () => applyFilters(false));
    ["type-filter", "tag-filter", "directory-filter"].forEach(id =>
      document.getElementById(id).addEventListener("change", () => applyFilters(true)));
    document.getElementById("layout").addEventListener("change", runLayout);
    applyFilters(true);
  })();
  </script>
  <footer hidden>Cytoscape.js __CYTOSCAPE_VERSION__ — MIT License</footer>
</body>
</html>
"""
