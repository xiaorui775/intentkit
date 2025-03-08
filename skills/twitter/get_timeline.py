import logging
from typing import Type

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from clients.twitter import get_twitter_client

from .base import Tweet, TwitterBaseTool

logger = logging.getLogger(__name__)


class TwitterGetTimelineInput(BaseModel):
    """Input for TwitterGetTimeline tool."""


class TwitterGetTimeline(TwitterBaseTool):
    """Tool for getting the user's timeline from Twitter.

    This tool uses the Twitter API v2 to retrieve tweets from the authenticated user's
    timeline.

    Attributes:
        name: The name of the tool.
        description: A description of what the tool does.
        args_schema: The schema for the tool's input arguments.
    """

    name: str = "twitter_get_timeline"
    description: str = "Get tweets from the authenticated user's timeline"
    args_schema: Type[BaseModel] = TwitterGetTimelineInput

    async def _arun(self, config: RunnableConfig = None, **kwargs) -> list[Tweet]:
        """Async implementation of the tool to get the user's timeline.

        Args:
            input (TwitterGetTimelineInput): The input for the tool.
            config (RunnableConfig): The configuration for the runnable, containing agent context.

        Returns:
            list[Tweet]: A list of tweets from the user's timeline.

        Raises:
            Exception: If there's an error accessing the Twitter API.
        """
        try:
            # Ensure max_results is an integer
            max_results = 10

            context = self.context_from_config(config)
            twitter = get_twitter_client(
                agent_id=context.agent.id,
                skill_store=self.skill_store,
                config=context.config,
            )

            # Check rate limit only when not using OAuth
            if not twitter.use_key:
                await self.check_rate_limit(
                    context.agent.id, max_requests=3, interval=60 * 24
                )

            # get since id from store
            last = await self.skill_store.get_agent_skill_data(
                context.agent.id, self.name, "last"
            )
            last = last or {}
            since_id = last.get("since_id")

            client = await twitter.get_client()
            user_id = twitter.self_id
            if not user_id:
                raise ValueError("Failed to get Twitter user ID.")

            timeline = await client.get_users_tweets(
                user_auth=twitter.use_key,
                id=user_id,
                max_results=max_results,
                since_id=since_id,
                expansions=[
                    "referenced_tweets.id",
                    "attachments.media_keys",
                    "author_id",
                ],
                tweet_fields=[
                    "created_at",
                    "author_id",
                    "text",
                    "referenced_tweets",
                    "attachments",
                ],
                user_fields=[
                    "username",
                    "name",
                    "description",
                    "public_metrics",
                    "location",
                    "connection_status",
                ],
                media_fields=["url"],
            )

            result = self.process_tweets_response(timeline)

            # Update the since_id in store for the next request
            if timeline.get("meta") and timeline["meta"].get("newest_id"):
                last["since_id"] = timeline["meta"]["newest_id"]
                await self.skill_store.save_agent_skill_data(
                    context.agent.id, self.name, "last", last
                )

            return result

        except Exception as e:
            logger.error("Error getting timeline: %s", str(e))
            raise type(e)(f"[agent:{context.agent.id}]: {e}") from e
