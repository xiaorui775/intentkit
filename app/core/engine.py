"""AI Agent Management Module.

This module provides functionality for initializing and executing AI agents. It handles:
- Agent initialization with LangChain
- Tool and skill management
- Agent execution and response handling
- Memory management with PostgreSQL
- Integration with CDP and Twitter

The module uses a global cache to store initialized agents for better performance.
"""

import logging
import textwrap
import time
from datetime import datetime

import sqlalchemy
from cdp_langchain.agent_toolkits import CdpToolkit
from cdp_langchain.utils import CdpAgentkitWrapper
from epyxid import XID
from fastapi import HTTPException
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
)
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.graph import CompiledGraph
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.exc import NoResultFound

from abstracts.graph import AgentState
from app.config.config import config
from app.core.agent import AgentStore
from app.core.graph import create_agent
from app.core.skill import SkillStore
from app.services.twitter.client import TwitterClient
from models.agent import Agent, AgentData
from models.chat import AuthorType, ChatMessage, ChatMessageSkillCall
from models.db import get_pool, get_session
from models.skill import AgentSkillData, ThreadSkillData
from skill_sets import get_skill_set
from skills.acolyt import get_Acolyt_skill
from skills.allora import get_allora_skill
from skills.common import get_common_skill
from skills.crestal import get_crestal_skill
from skills.enso import get_enso_skill
from skills.goat import (
    create_smart_wallets_if_not_exist,
    crossmint_chains,
    get_goat_skill,
    init_smart_wallets,
)
from skills.twitter import get_twitter_skill

logger = logging.getLogger(__name__)

# Global variable to cache all agent executors
agents: dict[str, CompiledGraph] = {}

# Global dictionaries to cache agent update times
agents_updated: dict[str, datetime] = {}


def agent_prompt(agent: Agent) -> str:
    prompt = "# SYSTEM PROMPT\n\n"
    if config.system_prompt:
        prompt += config.system_prompt + "\n\n"
    if agent.name:
        prompt += f"Your name is {agent.name}.\n"
    if agent.ticker:
        prompt += f"Your ticker symbol is {agent.ticker}.\n"
    prompt += "\n"
    if agent.purpose:
        prompt += f"## Purpose\n\n{agent.purpose}\n\n"
    if agent.personality:
        prompt += f"## Personality\n\n{agent.personality}\n\n"
    if agent.principles:
        prompt += f"## Principles\n\n{agent.principles}\n\n"
    if agent.prompt:
        prompt += f"## Initial Rules\n\n{agent.prompt}\n\n"
    elif agent.cdp_enabled:
        prompt += (
            "## Coinbase AgentKit Skills Guide\n\n"
            "You are a helpful agent that can interact onchain using the Coinbase Developer Platform AgentKit. "
            "You are empowered to interact onchain using your tools. If you ever need funds, you can request "
            "them from the faucet if you are on network ID 'base-mainnet'. If not, you can provide your wallet "
            "details and request funds from the user. Before executing your first action, get the wallet details "
            "to see what network you're on. If there is a 5XX (internal) HTTP error code, ask the user to try "
            "again later. If someone asks you to do something you can't do with your currently available tools, "
            "you must say so, and encourage them to implement it themselves using the CDP SDK + Agentkit, "
            "recommend they go to docs.cdp.coinbase.com for more information. Be concise and helpful with your "
            "responses. Refrain from restating your tools' descriptions unless it is explicitly requested."
            "\n\nWallet addresses are public information. If someone asks for your default wallet, current wallet, "
            "personal wallet, crypto wallet, or wallet public address, don't use any address in message history, "
            "you must use the 'get_wallet_details' tool to retrieve your wallet address every time."
        )
    if agent.enso_enabled:
        prompt += """## ENSO Skills Guide\n\nYou are integrated with the Enso API. You can use enso_get_tokens to retrieve token information,
        including APY, Protocol Slug, Symbol, Address, Decimals, and underlying tokens. When interacting with token amounts,
        ensure to multiply input amounts by the token's decimal places and divide output amounts by the token's decimals. 
        Utilize enso_route_shortcut to find the best swap or deposit route. Set broadcast_request to True only when the 
        user explicitly requests a transaction broadcast. Insufficient funds or insufficient spending approval can cause 
        Route Shortcut broadcasts to fail. To avoid this, use the enso_broadcast_wallet_approve tool that requires explicit 
        user confirmation before broadcasting any approval transactions for security reasons.\n\n"""
    if agent.goat_enabled:
        prompt += """## GOAT Skills Guide\n\nYou're using the Great Onchain Agent Toolkit (GOAT) SDK, which provides tools for DeFi, minting, betting, and analytics.
        GOAT supports EVM blockchains and various wallets, including keypairs, smart wallets, LIT, and MPC.\n\n"""
    return prompt


async def initialize_agent(aid):
    """Initialize an AI agent with specified configuration and tools.

    This function:
    1. Loads agent configuration from database
    2. Initializes LLM with specified model
    3. Loads and configures requested tools
    4. Sets up PostgreSQL-based memory
    5. Creates and caches the agent

    Args:
        aid (str): Agent ID to initialize

    Returns:
        Agent: Initialized LangChain agent

    Raises:
        HTTPException: If agent not found (404) or database error (500)
    """
    """Initialize the agent with CDP Agentkit."""
    # init skill store first
    skill_store = SkillStore()
    # init agent store
    agent_store = AgentStore(aid)

    # get the agent from the database
    try:
        agent: Agent = await agent_store.get_config()
        agent_data: AgentData = await agent_store.get_data()

        # Cache the update times
        agents_updated[aid] = agent.updated_at
    except NoResultFound:
        # Handle the case where the user is not found
        raise HTTPException(status_code=404, detail="Agent not found")
    except SQLAlchemyError as e:
        # Handle other SQLAlchemy-related errors
        logger.error(e)
        raise HTTPException(status_code=500, detail=str(e))

    # ==== Initialize LLM.
    input_token_limit = 120000
    # TODO: model name whitelist
    if agent.model.startswith("deepseek"):
        llm = ChatOpenAI(
            model_name=agent.model,
            openai_api_key=config.deepseek_api_key,
            openai_api_base="https://api.deepseek.com",
            frequency_penalty=agent.frequency_penalty,
            presence_penalty=agent.presence_penalty,
            temperature=agent.temperature,
            timeout=180,
        )
        input_token_limit = 60000
    else:
        llm = ChatOpenAI(
            model_name=agent.model,
            openai_api_key=config.openai_api_key,
            frequency_penalty=agent.frequency_penalty,
            presence_penalty=agent.presence_penalty,
            temperature=agent.temperature,
            timeout=180,
        )

    # ==== Store buffered conversation history in memory.
    memory = AsyncPostgresSaver(get_pool())

    # ==== Load skills
    tools: list[BaseTool] = []

    agentkit: CdpAgentkitWrapper
    # Configure CDP Agentkit Langchain Extension.
    if agent.cdp_enabled:
        values = {
            "cdp_api_key_name": config.cdp_api_key_name,
            "cdp_api_key_private_key": config.cdp_api_key_private_key,
            "network_id": getattr(agent, "cdp_network_id", "base-mainnet"),
        }
        if agent_data and agent_data.cdp_wallet_data:
            values["cdp_wallet_data"] = agent_data.cdp_wallet_data
        agentkit = CdpAgentkitWrapper(**values)
        # save the wallet after first create
        if not agent_data or not agent_data.cdp_wallet_data:
            await agent_store.set_data(
                {
                    "cdp_wallet_data": agentkit.export_wallet(),
                }
            )
        # Initialize CDP Agentkit Toolkit and get tools.
        cdp_toolkit = CdpToolkit.from_cdp_agentkit_wrapper(agentkit)
        cdp_tools = cdp_toolkit.get_tools()
        # Filter the tools to only include the ones that in agent.cdp_skills.
        if agent.cdp_skills and len(agent.cdp_skills) > 0:
            cdp_tools = [tool for tool in cdp_tools if tool.name in agent.cdp_skills]
            tools.extend(cdp_tools)

    if agent.goat_enabled and agent.crossmint_config:
        if (
            agent.crossmint_config.get("chains")
            and len(agent.crossmint_config.get("chains")) > 0
        ):
            chain_configs = {}
            for chain_name in agent.crossmint_config["chains"]:
                chain_config = crossmint_chains.get(chain_name)
                if chain_config:
                    chain_configs[chain_name] = chain_config

            if len(chain_configs) > 0:
                crossmint_wallet_data = (
                    agent_data.crossmint_wallet_data
                    if agent_data.crossmint_wallet_data
                    else {}
                )
                try:
                    smart_wallet_data = create_smart_wallets_if_not_exist(
                        config.crossmint_api_key,
                        crossmint_wallet_data.get("smart"),
                    )

                    # save the wallet after first create
                    if (
                        not crossmint_wallet_data
                        or not crossmint_wallet_data.get("smart")
                        or not crossmint_wallet_data.get("smart").get("evm")
                        or not crossmint_wallet_data.get("smart")
                        .get("evm")
                        .get("address")
                    ):
                        await agent_store.set_data(
                            {
                                "crossmint_wallet_data": {"smart": smart_wallet_data},
                            }
                        )

                    # give rpc some time to prevent error #429
                    time.sleep(1)

                    evm_crossmint_wallets = init_smart_wallets(
                        config.crossmint_api_key,
                        chain_configs,
                        smart_wallet_data["evm"],
                    )

                    for wallet in evm_crossmint_wallets:
                        try:
                            s = get_goat_skill(
                                wallet,
                                agent.goat_skills,
                                skill_store,
                                agent_store,
                                aid,
                            )
                            tools.extend(s)
                        except Exception as e:
                            logger.warning(e)
                except Exception as e:
                    logger.warning(e)
            else:
                logger.warning(
                    "No chain configs found for the configured crossmint chains."
                )

    # Enso skills
    if agent.enso_skills and len(agent.enso_skills) > 0 and agent.enso_config:
        for skill in agent.enso_skills:
            try:
                s = get_enso_skill(
                    skill,
                    agent.enso_config.get("api_token"),
                    agent.enso_config.get("main_tokens", list[str]()),
                    agentkit.wallet if agentkit else None,
                    config.rpc_base_mainnet,
                    skill_store,
                    agent_store,
                    aid,
                )
                tools.append(s)
            except Exception as e:
                logger.warning(e)
    # Acolyt skills
    if agent.acolyt_skills and len(agent.acolyt_skills) > 0 and agent.acolyt_config:
        for skill in agent.acolyt_skills:
            try:
                s = get_Acolyt_skill(
                    skill,
                    agent.acolyt_config.get("api_key"),
                    skill_store,
                    agent_store,
                    aid,
                )
                tools.append(s)
            except Exception as e:
                logger.warning(e)
    # Allora skills
    if agent.allora_skills and len(agent.allora_skills) > 0 and agent.allora_config:
        for skill in agent.allora_skills:
            try:
                s = get_allora_skill(
                    skill,
                    agent.allora_config.get("api_key"),
                    skill_store,
                    agent_store,
                    aid,
                )
                tools.append(s)
            except Exception as e:
                logger.warning(e)
    # Twitter skills
    twitter_prompt = ""
    if agent.twitter_skills and len(agent.twitter_skills) > 0:
        if not agent.twitter_config:
            agent.twitter_config = {}
        twitter_client = TwitterClient(aid, agent_store, agent.twitter_config)
        for skill in agent.twitter_skills:
            s = get_twitter_skill(
                skill,
                twitter_client,
                skill_store,
                aid,
                agent_store,
            )
            tools.append(s)
        twitter_prompt = (
            f"\n\nYour twitter id is {agent_data.twitter_id}, never reply or retweet yourself. "
            f"Your twitter username is {agent_data.twitter_username}. \n"
            f"Your twitter name is {agent_data.twitter_name}. \n"
        )

    # Crestal skills
    if agent.crestal_skills:
        for skill in agent.crestal_skills:
            tools.append(get_crestal_skill(skill))

    # Common skills
    if agent.common_skills:
        for skill in agent.common_skills:
            tools.append(get_common_skill(skill))

    # Skill sets
    if agent.skill_sets:
        for skill_set, opts in agent.skill_sets.items():
            tools.extend(get_skill_set(skill_set, opts))

    # filter the duplicate tools
    tools = list({tool.name: tool for tool in tools}.values())

    # log all tools
    for tool in tools:
        logger.info(f"[{aid}] loaded tool: {tool.name}")

    # finally, setup the system prompt
    prompt = agent_prompt(agent)
    # Escape curly braces in the prompt
    escaped_prompt = prompt.replace("{", "{{").replace("}", "}}")
    prompt_array = [
        ("system", escaped_prompt),
        ("placeholder", "{messages}"),
    ]
    if twitter_prompt:
        # deepseek only supports system prompt in the beginning
        if agent.model.startswith("deepseek"):
            # prompt_array.insert(0, ("system", twitter_prompt))
            pass
        else:
            prompt_array.append(("system", twitter_prompt))
    if agent.prompt_append:
        # Escape any curly braces in prompt_append
        escaped_append = agent.prompt_append.replace("{", "{{").replace("}", "}}")
        if agent.model.startswith("deepseek"):
            prompt_array.insert(0, ("system", escaped_append))
        else:
            prompt_array.append(("system", escaped_append))
    prompt_temp = ChatPromptTemplate.from_messages(prompt_array)

    def formatted_prompt(state: AgentState):
        # logger.debug(f"[{aid}] formatted prompt: {state}")
        return prompt_temp.invoke({"messages": state["messages"]})

    # hack for deepseek, it doesn't support tools
    if agent.model.startswith("deepseek"):
        tools = []

    # Create ReAct Agent using the LLM and CDP Agentkit tools.
    agents[aid] = create_agent(
        aid,
        llm,
        tools=tools,
        checkpointer=memory,
        state_modifier=formatted_prompt,
        debug=config.debug_checkpoint,
        input_token_limit=input_token_limit,
    )


async def agent_executor(agent_id: str) -> (CompiledGraph, float):
    start = time.perf_counter()
    agent = await Agent.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check if agent needs reinitialization due to updates
    needs_reinit = False
    if agent_id in agents:
        if (
            agent_id not in agents_updated
            or agent.updated_at != agents_updated[agent_id]
        ):
            needs_reinit = True
            logger.info(f"Reinitializing agent {agent_id} due to updates")

    # cold start or needs reinitialization
    cold_start_cost = 0.0
    if (agent_id not in agents) or needs_reinit:
        await initialize_agent(agent_id)
        cold_start_cost = time.perf_counter() - start
    return agents[agent_id], cold_start_cost


async def execute_agent(message: ChatMessage, debug: bool = False) -> list[ChatMessage]:
    """
    Execute an agent with the given prompt and return response lines.

    This function:
    1. Configures execution context with thread ID
    2. Initializes agent if not in cache
    3. Streams agent execution results
    4. Formats and times the execution steps

    Args:
        message (ChatMessage): The chat message containing agent_id, chat_id, and message content
        debug (bool): Enable debug mode, will save the skill results

    Returns:
        list[ChatMessage]: Formatted response lines including timing information
    """
    await message.save()

    thread_id = f"{message.agent_id}-{message.chat_id}"

    stream_config = {"configurable": {"thread_id": thread_id}}
    resp = []
    start = time.perf_counter()

    executor, cold_start_cost = await agent_executor(message.agent_id)
    last = start + cold_start_cost

    # Extract images from attachments
    image_urls = []
    if message.attachments:
        image_urls = [
            att.url
            for att in message.attachments
            if hasattr(att, "type") and att.type == "image" and hasattr(att, "url")
        ]

    # message
    content = [
        {"type": "text", "text": message.message},
    ]
    content.extend(
        [
            {"type": "image_url", "image_url": {"url": image_url}}
            for image_url in image_urls
        ]
    )

    # run
    cached_tool_step = None
    async for chunk in executor.astream(
        {"messages": [HumanMessage(content=content)]}, stream_config
    ):
        try:
            this_time = time.perf_counter()
            logger.debug(f"stream chunk: {chunk}", extra={"thread_id": thread_id})
            if "agent" in chunk and "messages" in chunk["agent"]:
                if len(chunk["agent"]["messages"]) != 1:
                    logger.error(
                        "unexpected agent message: " + str(chunk["agent"]["messages"]),
                        extra={"thread_id": thread_id},
                    )
                msg = chunk["agent"]["messages"][0]
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    # tool calls, save for later use
                    cached_tool_step = msg
                elif hasattr(msg, "content") and msg.content:
                    # agent message
                    chat_message = ChatMessage(
                        id=str(XID()),
                        agent_id=message.agent_id,
                        chat_id=message.chat_id,
                        author_id=message.agent_id,
                        author_type=AuthorType.AGENT,
                        message=msg.content,
                        input_tokens=(
                            msg.usage_metadata.get("input_tokens", 0)
                            if hasattr(msg, "usage_metadata") and msg.usage_metadata
                            else 0
                        ),
                        output_tokens=(
                            msg.usage_metadata.get("output_tokens", 0)
                            if hasattr(msg, "usage_metadata") and msg.usage_metadata
                            else 0
                        ),
                        time_cost=this_time - last,
                    )
                    last = this_time
                    if cold_start_cost > 0:
                        chat_message.cold_start_cost = cold_start_cost
                        cold_start_cost = 0
                    resp.append(chat_message)
                    await chat_message.save()
                else:
                    logger.error(
                        "unexpected agent message: " + str(msg),
                        extra={"thread_id": thread_id},
                    )
            elif "tools" in chunk and "messages" in chunk["tools"]:
                if not cached_tool_step:
                    logger.error(
                        "unexpected tools message: " + str(chunk["tools"]),
                        extra={"thread_id": thread_id},
                    )
                    continue
                skill_calls = []
                for msg in chunk["tools"]["messages"]:
                    if not hasattr(msg, "tool_call_id"):
                        logger.error(
                            "unexpected tools message: " + str(chunk["tools"]),
                            extra={"thread_id": thread_id},
                        )
                        continue
                    for call in cached_tool_step.tool_calls:
                        if call["id"] == msg.tool_call_id:
                            skill_call: ChatMessageSkillCall = {
                                "name": call["name"],
                                "parameters": call["args"],
                                "success": True,
                            }
                            if msg.status == "error":
                                skill_call["success"] = False
                                skill_call["error_message"] = msg.content
                            else:
                                if debug:
                                    skill_call["response"] = msg.content
                                else:
                                    skill_call["response"] = textwrap.shorten(
                                        msg.content, width=100, placeholder="..."
                                    )
                            skill_calls.append(skill_call)
                            break
                skill_message = ChatMessage(
                    id=str(XID()),
                    agent_id=message.agent_id,
                    chat_id=message.chat_id,
                    author_id=message.agent_id,
                    author_type=AuthorType.SKILL,
                    message="",
                    skill_calls=skill_calls,
                    input_tokens=(
                        cached_tool_step.usage_metadata.get("input_tokens", 0)
                        if hasattr(cached_tool_step, "usage_metadata")
                        and cached_tool_step.usage_metadata
                        else 0
                    ),
                    output_tokens=(
                        cached_tool_step.usage_metadata.get("output_tokens", 0)
                        if hasattr(cached_tool_step, "usage_metadata")
                        and cached_tool_step.usage_metadata
                        else 0
                    ),
                    time_cost=this_time - last,
                )
                last = this_time
                if cold_start_cost > 0:
                    skill_message.cold_start_cost = cold_start_cost
                    cold_start_cost = 0
                cached_tool_step = None
                resp.append(skill_message)
                await skill_message.save()
            elif "memory_manager" in chunk:
                pass
            else:
                logger.error(
                    "unexpected message type: " + str(chunk),
                    extra={"thread_id": thread_id},
                )
        except Exception as e:
            logger.error(
                f"failed to execute agent: {str(e)}", extra={"thread_id": thread_id}
            )
            error_message = ChatMessage(
                id=str(XID()),
                agent_id=message.agent_id,
                chat_id=message.chat_id,
                author_id=message.agent_id,
                author_type=AuthorType.SYSTEM,
                message=f"Error in agent:\n  {str(e)}",
                time_cost=time.perf_counter() - start,
            )
            await error_message.save()
            resp.append(error_message)
            return resp
    return resp


async def clean_agent_memory(
    agent_id: str,
    thread_id: str = "",
    clean_agent_memory: bool = False,
    clean_skills_memory: bool = False,
    debug: bool = False,
) -> str:
    """
    Clean an agent's memory with the given prompt and return response.

    This function:
    1. Cleans the agents skills data.
    2. Cleans the thread skills data.
    3. Cleans the graph checkpoint data.
    4. Cleans the graph checkpoint_writes data.
    5. Cleans the graph checkpoint_blobs data.

    Args:
        agent_id (str): Agent ID
        thread_id (str): Thread ID for the agent memory cleanup
        clean_agent_memory (bool): Whether to clean agent's memory data
        clean_skills_memory (bool): Whether to clean skills memory data
        debug (bool): Enable debug mode

    Returns:
        str: Successful response message.
    """
    # get the agent from the database
    async with get_session() as db:
        try:
            if not clean_skills_memory and not clean_agent_memory:
                raise HTTPException(
                    status_code=400,
                    detail="at least one of skills data or agent memory should be true.",
                )

            if clean_skills_memory:
                await AgentSkillData.clean_data(agent_id)
                await ThreadSkillData.clean_data(agent_id, thread_id)

            if clean_agent_memory:
                thread_id = thread_id.strip()
                q_suffix = "%"
                if thread_id and thread_id != "":
                    q_suffix = thread_id

                deletion_param = {"value": agent_id + "-" + q_suffix}
                await db.execute(
                    sqlalchemy.text(
                        "DELETE FROM checkpoints WHERE thread_id like :value",
                    ),
                    deletion_param,
                )
                await db.execute(
                    sqlalchemy.text(
                        "DELETE FROM checkpoint_writes WHERE thread_id like :value",
                    ),
                    deletion_param,
                )
                await db.execute(
                    sqlalchemy.text(
                        "DELETE FROM checkpoint_blobs WHERE thread_id like :value",
                    ),
                    deletion_param,
                )

            await db.commit()

            return "Agent data cleaned up successfully."
        except SQLAlchemyError as e:
            # Handle other SQLAlchemy-related errors
            logger.error(e)
            raise HTTPException(status_code=500, detail=str(e))
        except Exception as e:
            logger.error("failed to cleanup the agent memory: " + str(e))
            raise e


async def thread_stats(agent_id: str, chat_id: str) -> list[BaseMessage]:
    thread_id = f"{agent_id}-{chat_id}"
    stream_config = {"configurable": {"thread_id": thread_id}}
    try:
        executor, _ = await agent_executor(agent_id)
        snap = await executor.aget_state(stream_config)
        if snap.values and "messages" in snap.values:
            return snap.values["messages"]
        else:
            return []
    except Exception as e:
        logger.error(f"failed to get {thread_id} debug prompt: {e}")
        raise HTTPException(status_code=400, detail=str(e))
