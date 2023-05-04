import asyncio

from abc import abstractmethod
from typing import Any, Dict, List, Literal, cast


from agent.base import AgentInteraction
from agent.output.parse_output import ToolLog
from agent.output.output_stream_parser import OutputStreamParser, Step
from agent.output.token_callback_handler import TokenCallbackHandler
from agent.output.work_queue import WorkQueue
from langchain.callbacks.base import AsyncCallbackManager
from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler
from langchain.chains.llm import LLMChain
from langchain.schema import BaseMessage
from langchain.tools import BaseTool
from langchain.prompts.chat import (
    AIMessagePromptTemplate,
    BaseMessagePromptTemplate,
    ChatPromptTemplate,
    HumanMessagePromptTemplate,
    SystemMessagePromptTemplate,
)
from langchain.agents.tools import InvalidTool

from agent.base import AgentLog
from agent.tools.base import create_tools
from models.base import ModelConfig, PromptPart, get_model
from agent.base import AgentBase, AgentConfig
from database import db
from session import NodeJSPlayground


class RewriteStepsException(Exception):
    pass


class BasicAgent(AgentBase):
    def __init__(self, config: AgentConfig):
        super().__init__()
        self.config = config
        self.should_pause = False
        self.canceled = False
        self.rewriting_steps = False
        self.llm_generation = None
        self.can_resume = asyncio.Event()

    @classmethod
    async def create(cls, config: AgentConfig):
        c = cls(config=config)
        print("init")
        return c

    @staticmethod
    async def _run_tool(
        tool_log: ToolLog,
        name_to_tool_map: Dict[str, BaseTool],
    ) -> str:
        # Otherwise we lookup the tool
        if tool_log["tool_name"] in name_to_tool_map:
            tool = name_to_tool_map[tool_log["tool_name"]]
            # We then call the tool on the tool input to get an observation
            observation = await tool.arun(
                tool_log["tool_input"],
                verbose=True,
            )
        else:
            observation = await InvalidTool().arun(  # type: ignore
                tool_name=tool_log["tool_name"],
                tool_input=tool_log["tool_input"],
                verbose=True,
            )
        return observation

    @staticmethod
    def _get_prompt_part(
        prompts: List[PromptPart],
        role: Literal["user", "system"],
        type: str,
    ):
        return (
            next(
                prompt
                for prompt in prompts
                if prompt.role == role and prompt.type == type
            )
            # Double the parenthesses to escape them because the string will be prompt templated.
            .content.replace("{", "{{").replace("}", "}}")
        )

    @staticmethod
    def _create_prompt(model_config: ModelConfig, tools: List[BaseTool]):
        system_template = "\n\n".join(
            [
                BasicAgent._get_prompt_part(model_config.prompt, "system", "prefix"),
                "\n".join([f"{tool.name}: {tool.description}" for tool in tools]),
                BasicAgent._get_prompt_part(model_config.prompt, "system", "suffix"),
            ]
        )
        human_template = "\n\n".join(
            [
                BasicAgent._get_prompt_part(model_config.prompt, "user", "prefix"),
                "{agent_scratchpad}",
            ]
        )

        messages: List[BaseMessage | BaseMessagePromptTemplate] = [
            SystemMessagePromptTemplate.from_template(system_template),
            HumanMessagePromptTemplate.from_template(human_template),
            # TODO: Check if the results are better when we add scratchpad as AI message
            # AIMessagePromptTemplate.from_template("{agent_scratchpad}"),
        ]
        return ChatPromptTemplate(
            input_variables=["agent_scratchpad"],
            messages=messages,
        )

    async def _run(
        self,
        project_id: str,
        model_config: ModelConfig,
    ):
        playground = None
        try:
            # -----
            # Initialize callback manager and handler for controlling token stream
            callback_manager = AsyncCallbackManager([StreamingStdOutCallbackHandler()])
            self.token_handler = TokenCallbackHandler()
            callback_manager.add_handler(self.token_handler)

            # -----
            # Create tools
            # Create playground for code tools
            playground = NodeJSPlayground(get_envs=lambda: db.get_env_vars(project_id))
            tools = list(create_tools(playground=playground))
            self.tool_names = [tool.name for tool in tools]
            tool_map = {tool.name: tool for tool in tools}

            # Assign callback manager to tools
            for tool in tools:
                tool.callback_manager = callback_manager

            # -----
            # Create LLM from specified model
            llm_chain = LLMChain(
                llm=get_model(model_config, callback_manager),
                prompt=self._create_prompt(model_config, tools),
                callback_manager=callback_manager,
            )

            # -----
            # Create log handlers
            # Used for sending logs to db/frontend without blocking
            steps_streamer = WorkQueue[List[Step]](
                on_workload=lambda steps: self.config.on_logs(
                    [AgentLog(data=step) for step in steps]
                ),
            )

            def stream_steps(steps: List[Step]):
                steps_streamer.schedule(steps)

            # Used for parsing token stream into logs+actions
            self._output_parser = OutputStreamParser(tool_names=self.tool_names)

            # This function is used to inject information from previous steps to the current prompt
            # TODO: Improve the scratchpad handling and prompts for templates
            def get_agent_scratchpad():
                steps = self._output_parser.get_steps()

                if (
                    len(steps) == 0
                    or len(steps) == 1
                    and not self._output_parser._token_buffer
                ):
                    return ""

                agent_scratchpad = ""
                for step in steps:
                    agent_scratchpad += step["output"]

                    tool_logs = (
                        cast(ToolLog, action)
                        for action in step["logs"]
                        if action["type"] == "tool"
                    )

                    tool_outputs = "\n".join(
                        log.get("tool_output", "")
                        for log in tool_logs
                        if log.get("tool_output")
                    )

                    if tool_outputs:
                        agent_scratchpad += f"\nObservation: {tool_outputs}\nThought:"

                return f"This is your previous work (you can continue where you left off):\n{agent_scratchpad}"

            print("Generating...", flush=True)
            while True:
                try:
                    await self._check_interrupt()
                    # Query the LLM in background
                    scratchpad = get_agent_scratchpad()
                    self.llm_generation = asyncio.create_task(
                        llm_chain.agenerate(
                            [
                                {
                                    "agent_scratchpad": scratchpad,
                                    "stop": "Observation:",
                                }
                            ]
                        )
                    )
                    while True:
                        await self._check_interrupt()
                        # Consume the tokens from LLM
                        token = await self.token_handler.retrieve_token()

                        if token is None:
                            break

                        await self._check_interrupt()
                        self._output_parser.ingest_token(token)
                        stream_steps(self._output_parser.get_steps())

                    current_step = self._output_parser.get_current_step()

                    # Check if the run is finished
                    if any(
                        keyword in current_step["output"]
                        for keyword in ("Final Answer", "FinalAnswer")
                    ):
                        break

                    for tool in (
                        cast(ToolLog, action)
                        for action in current_step["logs"]
                        if action["type"] == "tool"
                    ):
                        await self._check_interrupt()
                        tool_output = await self._run_tool(
                            tool,
                            name_to_tool_map=tool_map,
                        )
                        await self._check_interrupt()
                        self._output_parser.ingest_tool_output(tool_output)
                        stream_steps(self._output_parser.get_steps())

                except RewriteStepsException:
                    print("REWRITING")
                    continue

        except:
            raise
        finally:
            if playground is not None:
                playground.close()
            await self.config.on_close()

    async def _check_interrupt(self):
        if self.canceled:
            raise Exception("Canceled")

        if self.should_pause:
            await self.can_resume.wait()
            self.can_resume.clear()

        if self.rewriting_steps:
            self.rewriting_steps = False
            raise RewriteStepsException()

    async def start(self, project_id: str, model_config: Dict[str, Any]):
        print("Start agent run")
        self.project_id = project_id
        asyncio.create_task(
            self._run(
                project_id=project_id,
                model_config=ModelConfig(**model_config),
            )
        )

    async def interaction(self, interaction: AgentInteraction):
        print("Agent interaction")
        if interaction.type == "pause":
            await self._pause()
        elif interaction.type == "resume":
            await self._resume()
        elif interaction.type == "rewrite_steps":
            await self._rewrite_steps(interaction.data["steps"])
        else:
            raise Exception(f"Unknown interaction action: {interaction.type}")

    async def _pause(self):
        print("Pause agent run")
        self.should_pause = True

    async def _resume(self):
        print("Resume agent run")
        self.should_pause = False
        self.can_resume.set()

    async def stop(self):
        print("Cancel agent run")
        self.canceled = True
        if self.llm_generation:
            self.llm_generation.cancel()
        await self.config.on_close()

    async def _rewrite_steps(self, steps: List[Step]):
        print("Rewrite agent run steps")
        await self._pause()
        self.rewriting_steps = True
        if self.llm_generation:
            self.llm_generation.cancel()
        # TODO: Communicate the rewriting in model prompt - inform about what was rewritten.
        # TODO: Handle token that is generated after a rewrite - if the current step seems "finished" the model still outputs string (usually something like . or :).
        self._output_parser = OutputStreamParser(
            tool_names=self.tool_names,
            steps=steps[:-1],
            buffered_step=steps[-1],
        )
        await self._resume()
