from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from apipi.schemas import StrictModel

_UNIMPLEMENTED = ("multi_agent", "tool_search", "programmatic_tool_calling")


class FunctionTool(StrictModel):
    type: Literal["function"]
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class McpHttpTransport(StrictModel):
    type: Literal["http"]
    server_url: str


class McpStdioTransport(StrictModel):
    type: Literal["stdio"]
    command: str
    args: list[str] | None = None
    cwd: str | None = None


class McpTool(StrictModel):
    type: Literal["mcp"]
    server_label: str
    transport: Annotated[
        McpHttpTransport | McpStdioTransport, Field(discriminator="type")
    ]
    headers: dict[str, str] | None = None
    required: bool | None = None
    credential_id: str | None = None
    connection_origin: Literal["service", "environment"] | None = None

    @model_validator(mode="after")
    def origin_supported(self) -> Self:
        if self.connection_origin == "environment":
            raise PydanticCustomError(
                "not_implemented",
                "{field} is not implemented",
                {"field": "connection_origin"},
            )
        return self


AgentTool = Annotated[FunctionTool | McpTool, Field(discriminator="type")]


class AgentWrite(StrictModel):
    name: str | None = None
    model: str | None = None
    instructions: str | None = None
    metadata: dict[str, Any] | None = None
    tools: list[AgentTool] | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_unimplemented(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for field in _UNIMPLEMENTED:
                if field in data:
                    raise PydanticCustomError(
                        "not_implemented",
                        "{field} is not implemented",
                        {"field": field},
                    )
        return data
