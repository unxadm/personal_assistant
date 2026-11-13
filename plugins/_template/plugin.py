"""Plugin template. See docs/PLUGINS.md for the full guide."""

from __future__ import annotations

from typing import Any

from assistant.plugins.sdk import Item, Plugin, PluginError, tool


class MyPlugin(Plugin):
    async def setup(self) -> None:
        """Runs when the plugin is enabled or its settings change."""
        self.ctx.log.info("starting with config %s", self.ctx.config)

    async def sync(self) -> None:
        """Fetch data and normalize it into Items. Called every sync_interval seconds."""
        response = await self.ctx.http(
            "GET", "https://api.example.com/things",
            headers={"Authorization": f"Bearer {self.ctx.secret('api_token')}"},
        )
        response.raise_for_status()
        cursor = await self.ctx.kv_get("cursor")  # small persistent state, e.g. sync cursors
        items = [
            Item(external_id=str(thing["id"]), type="item", title=thing["name"], body=thing["text"])
            for thing in response.json()
        ]
        await self.ctx.upsert_items(items)  # unchanged items are skipped automatically
        await self.ctx.kv_set("cursor", cursor)
        if items:
            await self.ctx.emit("new_things", {"count": len(items)})

    @tool(
        "Say hello to someone. The description tells the model when to use the tool.",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        untrusted=False,  # output is our own text, not third-party content
    )
    async def greet(self, name: str) -> Any:
        if not name.strip():
            raise PluginError("name is empty")  # reported to the model as a tool error
        return {"message": f"{self.ctx.config['greeting']}, {name}!"}
