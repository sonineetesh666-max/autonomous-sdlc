"""Repo tools handled locally (the DX MCP server covers the org, not our git workspace)."""
from __future__ import annotations

import os
from pathlib import Path

from .loop import ToolText

SKIP_DIRS = {".git", "node_modules", ".sfdx", ".sf", ".localdevserver", "__pycache__", ".vscode"}
TEXT_EXT = {".cls", ".trigger", ".xml", ".js", ".html", ".css", ".json", ".md", ".yaml", ".yml",
            ".page", ".component", ".txt", ".apex", ".soql"}


def _safe(ws: Path, rel: str) -> Path:
    root = ws.resolve()
    p = (root / (rel or ".")).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"Path '{rel}' is outside the workspace")
    return p


def _walk(base: Path):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            yield Path(dirpath) / f


def file_tools(ws: Path, writable: bool, write_prefixes: tuple[str, ...] = ("force-app/",)):
    tools = [
        {"name": "list_files", "description": "List files in the Salesforce DX repo (relative paths).",
         "input_schema": {"type": "object", "properties": {
             "path": {"type": "string", "description": "Directory relative to repo root. Default '.'"}}}},
        {"name": "read_file", "description": "Read a text file from the repo.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "search_repo", "description": "Case-insensitive text search across repo source files. "
                                               "Returns file:line: snippet.",
         "input_schema": {"type": "object", "properties": {
             "query": {"type": "string"}, "path": {"type": "string"}}, "required": ["query"]}},
    ]
    if writable:
        tools.append({
            "name": "write_file",
            "description": "Create or overwrite a file with FULL content. Allowed only under: "
                           + ", ".join(write_prefixes),
            "input_schema": {"type": "object", "properties": {
                "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}})

    async def list_files(inp):
        base = _safe(ws, inp.get("path", "."))
        out = []
        for p in _walk(base):
            out.append(p.relative_to(ws).as_posix())
            if len(out) >= 400:
                out.append("... truncated; list a narrower path")
                break
        return ToolText("\n".join(out) or "(empty)")

    async def read_file(inp):
        p = _safe(ws, inp["path"])
        if not p.is_file():
            return ToolText(f"Not a file: {inp['path']}", True)
        text = p.read_text(errors="ignore")
        suffix = "\n... [truncated]" if len(text) > 60_000 else ""
        return ToolText(text[:60_000] + suffix)

    async def search_repo(inp):
        q = inp["query"].lower()
        hits = []
        for p in _walk(_safe(ws, inp.get("path", "."))):
            if p.suffix not in TEXT_EXT:
                continue
            for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                if q in line.lower():
                    hits.append(f"{p.relative_to(ws).as_posix()}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 80:
                        return ToolText("\n".join(hits + ["... more hits truncated"]))
        return ToolText("\n".join(hits) or "No matches.")

    async def write_file(inp):
        rel = inp["path"].replace("\\", "/").lstrip("/")
        if not rel.startswith(write_prefixes):
            return ToolText(f"Refused: writes allowed only under {write_prefixes}", True)
        p = _safe(ws, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(inp["content"])
        return ToolText(f"Wrote {len(inp['content'])} chars to {rel}")

    handlers = {"list_files": list_files, "read_file": read_file, "search_repo": search_repo}
    if writable:
        handlers["write_file"] = write_file
    return tools, handlers
