from typing import Any, Dict, List
from agent.output.output_stream_parser import Step

from database.client import Client
from session.env import EnvVar

TABLE_DEPLOYMENTS = "deployments"
TABLE_PROJECTS = "projects"


class Database:
    def __init__(self, supabase_url: str, supabase_key: str) -> None:
        self.client = Client(supabase_url=supabase_url, supabase_key=supabase_key)

    async def upsert_deployment(
        self,
        id: str,
        project_id: str | None = None,
        logs: List[Step] | None = None,
        enabled: bool | None = True,
    ) -> None:
        update: Dict[str, Any] = {
            "id": id,
            "enabled": enabled,
        }

        if project_id:
            update["project_id"] = project_id

        if logs:
            update["logs"] = logs

        await self.client.table(TABLE_DEPLOYMENTS).upsert(
            update,
            on_conflict="id",
        ).execute()

    async def update_project_developent_logs(
        self,
        id: str,
        logs: List[Step],
    ) -> None:
        await self.client.table(TABLE_PROJECTS).update(
            {
                "development_logs": logs,
            }
        ).eq(
            "id",
            id,
        ).execute()

    async def get_env_vars(self, project_id: str) -> List[EnvVar]:
        """The return value is a list of dicts with the following keys:
        - key: The name of an env var
        - value: The value of an env var

        Example of a return value:
        [
            {
                "key": "",
                "value": "",
            },
            {
                "key": "MY_TOKEN",
                "value": "my-token-value",
            }
        ]
        """
        result = (
            await self.client.table(TABLE_PROJECTS)
            .select("data")
            .eq("id", project_id)
            .execute()
        )
        state = result.data[0]["data"]["state"]
        if "envs" in state:
            return state["envs"]
        return []
