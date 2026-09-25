"""The agent loop.

Our app is the MCP HOST: it runs this loop, holds the Claude API client, and contains an MCP CLIENT
that connects to the Salesforce DX MCP SERVER (started as a subprocess, scoped to ONE org).

Claude sees two kinds of tools:
  * MCP tools   -> discovered from the DX MCP server at runtime, forwarded to it
  * local tools -> handled in Python (file access, ask_human, submit_* stage terminators)

Local handlers return one of:
  ToolText      -> normal tool result, loop continues
  PauseSignal   -> stop now, persist conversation, wait for a human (ask_human)
  FinishSignal  -> stage complete (submit_design / submit_build accepted)
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Union

from anthropic import AsyncAnthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("sdlc.agent")

MAX_TOOL_OUTPUT = 20_000
MAX_NUDGES = 3


@dataclass
class ToolText:
    text: str
    is_error: bool = False


@dataclass
class PauseSignal:
    payload: dict


@dataclass
class FinishSignal:
    payload: dict


Handler = Callable[[dict], Awaitable[Union[ToolText, PauseSignal, FinishSignal]]]


@dataclass
class AgentOutcome:
    kind: str                                   # "finished" | "paused" | "failed"
    payload: dict
    messages: list
    pending_tool_id: str | None = None
    partial_results: list = field(default_factory=list)


def tool_result(tool_use_id: str, text: str, is_error: bool = False) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id,
            "content": text[:MAX_TOOL_OUTPUT] or "(empty)", "is_error": is_error}


async def run_agent(*, model: str, max_tokens: int, system: str, messages: list,
                    local_tools: list[dict], handlers: dict[str, Handler],
                    mcp_params: StdioServerParameters | None, finish_tool: str,
                    max_turns: int = 80) -> AgentOutcome:
    client = AsyncAnthropic()
    async with AsyncExitStack() as stack:
        session: ClientSession | None = None
        mcp_tools: list[dict] = []

        if mcp_params is not None:
            read, write = await stack.enter_async_context(stdio_client(mcp_params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = await session.list_tools()
            mcp_tools = [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
                         for t in listed.tools]
            log.info("DX MCP tools available: %s", [t["name"] for t in mcp_tools])

        mcp_names = {t["name"] for t in mcp_tools}
        tools = mcp_tools + local_tools
        nudges = 0

        for turn in range(max_turns):
            resp = await client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                                tools=tools, messages=messages)
            messages.append({"role": "assistant",
                             "content": [b.model_dump(exclude_none=True) for b in resp.content]})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]

            if not tool_uses:
                nudges += 1
                if nudges > MAX_NUDGES:
                    return AgentOutcome("failed", {"reason": "Agent stopped without finishing the stage."}, messages)
                messages.append({"role": "user", "content":
                                 f"Continue working. This stage only ends when you call {finish_tool}."})
                continue

            results: list[dict] = []
            pause: tuple[str, dict] | None = None
            for b in tool_uses:
                log.info("turn %d tool %s", turn, b.name)
                if b.name in handlers:
                    out = await handlers[b.name](b.input or {})
                    if isinstance(out, FinishSignal):
                        return AgentOutcome("finished", out.payload, messages)
                    if isinstance(out, PauseSignal):
                        pause = (b.id, out.payload)
                        continue
                    results.append(tool_result(b.id, out.text, out.is_error))
                elif b.name in mcp_names and session is not None:
                    try:
                        r = await session.call_tool(b.name, b.input or {})
                        text = "\n".join(c.text for c in r.content if getattr(c, "type", "") == "text")
                        results.append(tool_result(b.id, text, bool(getattr(r, "isError", False))))
                    except Exception as e:                      # surface tool errors to Claude
                        results.append(tool_result(b.id, f"MCP tool error: {e}", True))
                else:
                    results.append(tool_result(b.id, f"Unknown tool '{b.name}'.", True))

            if pause:
                return AgentOutcome("paused", pause[1], messages,
                                    pending_tool_id=pause[0], partial_results=results)
            messages.append({"role": "user", "content": results})

        return AgentOutcome("failed", {"reason": f"Exceeded {max_turns} turns."}, messages)
