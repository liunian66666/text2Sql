from __future__ import annotations

import threading
import time
from .usage import record_usage
from .budget import BudgetExceededError, TruncatedResponseError, get_meter as _get_budget_meter
from typing import TYPE_CHECKING, Dict, List, Optional
from tenacity import(
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential
)
from openai import (
    OpenAI,
    AzureOpenAI,
    OpenAIError,
    AuthenticationError,
    RateLimitError,
    BadRequestError,
    APITimeoutError,
    APIConnectionError,
    InternalServerError
)
from openai.types.chat import ChatCompletionMessage
from app.logger import logger

if TYPE_CHECKING:
    from app.config.config import LLMConfig


class EmptyResponseError(Exception):
    """Custom exception for empty LLM responses, used to trigger retry."""
    pass


class LLM:
    """LLM wrapper class. Each instance lazily creates its own OpenAI client."""
    
    def __init__(self, llm_config: LLMConfig):
        self._config = llm_config
        self._client = None
        self._client_lock = threading.Lock()
        logger.debug(
            f"Initialized LLM wrapper: model={llm_config.model}, "
            f"temperature={llm_config.temperature}, reasoning_effort={llm_config.reasoning_effort}"
        )
    
    @property
    def llm_config(self) -> LLMConfig:
        """Get the LLM configuration."""
        return self._config
    
    def _create_client(self):
        if self._config.api_type == "openai":
            # Keep retries in this wrapper so the caller has one predictable
            # retry budget. The SDK otherwise retries again inside every
            # tenacity attempt and can exceed an interactive request deadline.
            return OpenAI(api_key=self._config.api_key, base_url=self._config.base_url, max_retries=0)
        elif self._config.api_type == "azure":
            return AzureOpenAI(
                api_key=self._config.api_key,
                base_url=self._config.base_url,
                api_version=self._config.api_version,
                max_retries=0,
            )
        else:
            raise ValueError(f"Unsupported api type: {self._config.api_type}")

    def _get_client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = self._create_client()
                    logger.debug(f"Created LLM client for model={self._config.model}")
        return self._client
        
    @retry(
        wait=wait_random_exponential(multiplier=1, max=60),
        stop=stop_after_attempt(3),
        # Retry on recoverable errors (including BadRequestError for provider-specific issues like "user location not supported")
        # NOT on AuthenticationError (wrong API key won't fix itself)
        retry=retry_if_exception_type((RateLimitError, APITimeoutError, APIConnectionError, InternalServerError, BadRequestError, EmptyResponseError))
    )
    def ask(self, messages: List[Dict[str, str]],
                  system_message: Optional[Dict[str, str]] = None,
                  timeout: int = 300,
                  **kwargs) -> tuple[List[ChatCompletionMessage], Dict[str, int]]:
        # Hard per-request guard: stop before adding load when the demo/skill
        # token or wall-clock budget is already exhausted. tenacity must NOT
        # retry this — BudgetExceededError is outside retry_if_exception_type.
        meter = _get_budget_meter()
        if meter is not None:
            meter.check()

        if system_message:
            messages = [system_message] + messages
            
        target_n = kwargs.pop("n", 1)
        max_request_n = 1 if self._config.n_call_strategy == "split" else (self._config.max_request_n or target_n)
        
        all_choices = []
        total_token_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        }
        
        current_max_tokens = kwargs.pop("max_tokens", self._config.max_tokens)
        
        while len(all_choices) < target_n:
            if meter is not None:
                meter.check()
            current_n = min(target_n - len(all_choices), max_request_n)
            try:
                request_params = {
                    "model": self._config.model,
                    "messages": messages,
                    "max_tokens": current_max_tokens,
                    "temperature": self._config.temperature,
                    "timeout": timeout,
                    "n": current_n,
                }
                if self._config.reasoning_effort is not None:
                    request_params["reasoning_effort"] = self._config.reasoning_effort
                if self._config.extra_body:
                    request_params["extra_body"] = self._config.extra_body
                request_params.update(kwargs)
                    
                request_start = time.perf_counter()
                response = self._get_client().chat.completions.create(**request_params)
                record_usage(response, model=self._config.model, elapsed_seconds=time.perf_counter() - request_start)
                if not response.choices:
                    raise EmptyResponseError(f"No response from the model: {response}")
                
                # Keep valid choices from partially successful multi-sample
                # responses. Some OpenAI-compatible reasoning servers may
                # return reasoning_content but no final content for only one
                # choice; discarding the whole response wastes the other
                # valid samples and their tokens.
                valid_messages = []
                for choice in response.choices:
                    if choice.message.content is None or choice.message.content.strip() == "":
                        logger.warning(
                            "Model returned an empty final content for one choice; "
                            "keeping other valid choices and resampling the missing choice"
                        )
                        continue
                    valid_messages.append(choice.message)

                if not valid_messages:
                    # If every choice was cut off by max_tokens (reasoning model
                    # burning the whole budget on thinking), retrying as-is just
                    # repeats the same deterministic failure — fail fast instead.
                    finish_reasons = {getattr(c, "finish_reason", None) for c in response.choices}
                    if finish_reasons and finish_reasons <= {"length"}:
                        raise TruncatedResponseError(
                            f"All {len(response.choices)} choices hit max_tokens "
                            f"({current_max_tokens}) without producing final content"
                        )
                    raise EmptyResponseError(
                        "Model returned no choices with non-empty final content"
                    )
                
                all_choices.extend(valid_messages)
                
                # Calculate token usage
                if response.usage:
                    total_token_usage["prompt_tokens"] += response.usage.prompt_tokens
                    total_token_usage["completion_tokens"] += response.usage.completion_tokens
                    total_token_usage["total_tokens"] += response.usage.total_tokens
                
            except OpenAIError as e:
                if isinstance(e, RateLimitError):
                    logger.error(f"OpenAI error: {e}")
                    logger.error("Rate limit exceeded, please try again later.")
                elif isinstance(e, AuthenticationError):
                    logger.error(f"OpenAI error: {e}")
                    logger.error("Authentication error, please check your api key.")
                elif isinstance(e, BadRequestError):
                    error_msg = str(e).lower()
                    if "context" in error_msg or "tokens" in error_msg or "length" in error_msg:
                        new_max_tokens = int(current_max_tokens * 0.9) if current_max_tokens else 0
                        if new_max_tokens > 0:
                            logger.warning(f"Context length exceeded. Reducing max_tokens from {current_max_tokens} to {new_max_tokens} and retrying.")
                            current_max_tokens = new_max_tokens
                            continue
                        else:
                            logger.error(f"Context length exceeded, but max_tokens cannot be reduced further (current: {current_max_tokens}).")
                    
                    logger.error(f"OpenAI error: {e}")
                    logger.error("Bad request, please check your request parameters.")
                raise e
            except BudgetExceededError:
                # Deliberate abort, not a fault: the request hit its hard cap or every
                # choice was truncated by max_tokens. tenacity will not retry this, so
                # log at WARNING to keep the run readable.
                logger.warning("Aborting request: budget exhausted (hard cap or all choices truncated)")
                raise
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                raise e
                
        return all_choices, total_token_usage
