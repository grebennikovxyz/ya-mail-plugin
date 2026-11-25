"""Export the Ya-Ma input fields used in n8n's operation editor."""

import asyncio
import json
from pathlib import Path

from ya_mail.server import mcp


def parameter(name, schema, required):
    spec = next((s for s in schema.get("anyOf", []) if s.get("type") != "null"), schema)
    kind = spec.get("type", "object")
    field = {"name": name, "type": kind}
    if spec.get("enum"):
        field["options"] = spec["enum"]
    if required:
        field["required"] = True
    if schema.get("default") is not None:
        field["default"] = schema["default"]
    for key in ("minimum", "maximum"):
        if key in spec:
            field[key] = spec[key]
    return field


async def export():
    data = {}
    for tool in await mcp.list_tools():
        schema = tool.inputSchema
        data[tool.name] = {
            "description": tool.description.split("\n")[0],
            "fields": [
                parameter(name, field, name in schema.get("required", []))
                for name, field in schema["properties"].items()
            ],
        }
    return data


if __name__ == "__main__":
    path = Path(__file__).resolve().parents[1] / "nodes/YaMa/catalog.json"
    path.write_text(
        json.dumps(asyncio.run(export()), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
