"""Direct API transports. Credentials are resolved at call time and never serialized."""

from __future__ import annotations

import base64
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from ..errors import FailureCategory, ProviderFailure
from ..output import CandidateMaterial
from ..usage import token_count
from .base import (
    IsolationMode,
    ProviderCapabilities,
    ProviderDiagnostic,
    ProviderExecution,
    ProviderTerminalKind,
    ProviderUsage,
    StructuredOutputMode,
    UsageAvailability,
)
from .materialized import materialized_prompt


@contextmanager
def _total_deadline(client, seconds):
    """Interrupt a streaming socket even if it continuously emits response bytes."""
    expired = threading.Event()
    deadline = time.monotonic() + seconds if seconds is not None else None
    response = [None, None]
    def expire():
        expired.set()
        stream = response[1]
        if stream is None and response[0] is not None:
            stream = response[0].extensions.get("network_stream")
        try:
            sock = stream.get_extra_info("socket") if stream is not None else None
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        try:
            client.close()
        except Exception:
            pass
    timer = threading.Timer(seconds, expire) if seconds is not None else None
    if timer is not None:
        timer.daemon = True
        timer.start()
    def check():
        if expired.is_set() or (deadline is not None and time.monotonic() >= deadline):
            raise ProviderFailure("API exceeded the total execution timeout.",
                category=FailureCategory.TIMEOUT, details={"code": "provider_total_timeout"})
    try:
        yield response, check
        check()
    except Exception:
        check()
        raise
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()


def _trace_network_stream(active, event, info):
    # httpcore exposes a connected stream before response headers are read.
    # Keep that socket interruptible through TLS, request writes and header waits.
    if event in {"connection.connect_tcp.complete", "connection.start_tls.complete"}:
        active[1] = info.get("return_value")


@dataclass(frozen=True)
class HTTPProviderConfig:
    name: str
    protocol: str
    base_url: str
    max_output_tokens: int = 8192
    reasoning_efforts: tuple[str, ...] = ()
    vision: bool = False

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        local_http = url.scheme == "http" and url.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
        if (
            not self.name
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or (url.scheme != "https" and not local_http)
        ):
            raise ValueError(
                "API base URL must be credential-free HTTPS or loopback HTTP."
            )
        if self.protocol not in {"responses", "chat-completions", "anthropic"}:
            raise ValueError("Unsupported API protocol.")
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or not 128 <= self.max_output_tokens <= 131072
        ):
            raise ValueError("max_output_tokens must be between 128 and 131072.")


class HTTPAPIAdapter:
    compatibility_version = "http-api.v1-materialized"

    def __init__(
        self,
        config: HTTPProviderConfig,
        *,
        credential: Callable[[], str | None],
        transport: Any = None,
    ) -> None:
        from dataclasses import asdict
        import hashlib
        import json

        self.config, self.credential, self.transport = config, credential, transport
        self.name = config.name
        self.compatibility_version += (
            ":"
            + hashlib.sha256(
                json.dumps(asdict(config), sort_keys=True).encode()
            ).hexdigest()
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            False,
            StructuredOutputMode.PROMPT,
            UsageAvailability.PARTIAL,
            IsolationMode.ISOLATED,
            IsolationMode.ISOLATED,
            True,
            False,
            tuple(self.config.reasoning_efforts),
        )

    def doctor(self) -> ProviderDiagnostic:
        try:
            import httpx  # noqa: F401

            available = True
        except ImportError:
            available = False
        return ProviderDiagnostic(
            self.name,
            available,
            None,
            {"protocol": self.config.protocol, "authentication": "not_checked"},
        )

    def start(self, request: Any, observer: Any, stop: Any) -> ProviderExecution:
        import httpx

        stop.raise_if_requested()
        prompt, images = materialized_prompt(request.workspace)
        if images and not self.config.vision:
            raise ProviderFailure(
                "Image input has not been enabled for this API model.",
                category=FailureCategory.INVALID_REQUEST,
            )
        effort = request.capabilities.get("reasoning_effort")
        if effort is not None and effort not in self.config.reasoning_efforts:
            raise ProviderFailure(
                "The reasoning effort is not declared supported by this provider configuration.",
                category=FailureCategory.INVALID_REQUEST,
            )
        secret = self.credential()
        if not secret:
            raise ProviderFailure(
                "API credential is unavailable. Unlock or configure the credential store.",
                category=FailureCategory.AUTHENTICATION,
            )
        body, suffix = self._body(request.model, prompt, images, effort)
        headers = (
            {"x-api-key": secret, "anthropic-version": "2023-06-01"}
            if self.config.protocol == "anthropic"
            else {"Authorization": f"Bearer {secret}"}
        )
        observer.progress("llm_provider_started", {"transport": self.config.protocol})
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=min(request.idle_timeout_seconds or 120, getattr(request, "total_timeout_seconds", None) or float("inf")),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with _total_deadline(client, getattr(request, "total_timeout_seconds", None)) as (active_response, check_deadline):
                    with client.stream(
                        "POST",
                        self.config.base_url.rstrip("/") + suffix,
                        json=body,
                        headers=headers,
                        extensions={"trace": lambda event, info: _trace_network_stream(active_response, event, info)},
                    ) as response:
                        active_response[0] = response
                        check_deadline()
                        if response.status_code >= 300:
                            category = {
                                401: FailureCategory.AUTHENTICATION,
                                403: FailureCategory.AUTHENTICATION,
                                429: FailureCategory.RATE_LIMIT,
                            }.get(
                                response.status_code,
                                (
                                    FailureCategory.INVALID_REQUEST
                                    if response.status_code < 500
                                    else FailureCategory.TRANSPORT
                                ),
                            )
                            retry = response.headers.get("retry-after", "")
                            raise ProviderFailure(
                                f"API returned HTTP {response.status_code}.",
                                category=category,
                                retryable=response.status_code in {429, 502, 503, 504},
                                retry_after_seconds=(
                                    float(retry) if retry.isdigit() else None
                                ),
                                details={"http_status": response.status_code},
                            )
                        payload = bytearray()
                        for chunk in response.iter_bytes():
                            check_deadline()
                            stop.raise_if_requested()
                            payload.extend(chunk)
                            if len(payload) > 16 * 1024 * 1024:
                                raise ProviderFailure(
                                    "API response exceeds 16 MiB.",
                                    category=FailureCategory.TRANSPORT,
                                )
                        import json

                        data = json.loads(payload)
        except httpx.TimeoutException as exc:
            raise ProviderFailure(
                "API request timed out; remote billing may have occurred.",
                category=FailureCategory.TIMEOUT, details={"code": "provider_idle_timeout"},
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderFailure(
                "API transport failed; remote billing may have occurred.",
                category=FailureCategory.TRANSPORT,
            ) from exc
        except (ValueError, TypeError) as exc:
            raise ProviderFailure(
                "API returned invalid JSON.", category=FailureCategory.TRANSPORT
            ) from exc
        stop.raise_if_requested()

        def scrub(value):
            if isinstance(value, str):
                return value.replace(secret, "[redacted]")
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, dict):
                return {scrub(k): scrub(v) for k, v in value.items()}
            return value

        try:
            if not isinstance(data, Mapping):
                raise ValueError("API response must be an object.")
            return self._result(scrub(data))
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise ProviderFailure(
                "API response does not match the selected transport protocol.",
                category=FailureCategory.TRANSPORT,
            ) from exc

    def resume(
        self, handle: Any, request: Any, observer: Any, stop: Any
    ) -> ProviderExecution:
        raise ProviderFailure(
            "Direct API sessions do not expose native resume handles.",
            category=FailureCategory.INVALID_REQUEST,
        )

    def _body(
        self, model: str, prompt: str, images: Any, effort: str | None
    ) -> tuple[dict[str, Any], str]:
        encoded = [
            (media, base64.b64encode(payload).decode("ascii"))
            for media, payload in images
        ]
        if self.config.protocol == "responses":
            content = [{"type": "input_text", "text": prompt}]
            content.extend(
                {"type": "input_image", "image_url": f"data:{media};base64,{value}"}
                for media, value in encoded
            )
            result = {
                "model": model,
                "input": [{"role": "user", "content": content}],
                "max_output_tokens": self.config.max_output_tokens,
                "store": False,
            }
            if effort is not None:
                result["reasoning"] = {"effort": effort}
            return result, "/responses"
        if self.config.protocol == "anthropic":
            content = [{"type": "text", "text": prompt}]
            content.extend(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media, "data": value},
                }
                for media, value in encoded
            )
            result = {
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": self.config.max_output_tokens,
            }
            if effort is not None:
                result["output_config"] = {"effort": effort}
            return result, "/messages"
        content = [{"type": "text", "text": prompt}]
        content.extend(
            {"type": "image_url", "image_url": {"url": f"data:{media};base64,{value}"}}
            for media, value in encoded
        )
        result = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_completion_tokens": self.config.max_output_tokens,
        }
        if effort is not None:
            result["reasoning_effort"] = effort
        return result, "/chat/completions"

    def _result(self, data: Mapping[str, Any]) -> ProviderExecution:
        usage = data.get("usage") or {}
        detail: dict[str, Any] = {
            "cache_write_tokens": 0,
            "reasoning_in_output": True,
            "input_includes_cache": True,
        }
        if self.config.protocol == "responses":
            complete = data.get("status") == "completed"
            text = "\n".join(
                c["text"]
                for item in data.get("output", [])
                if item.get("type") == "message"
                for c in item.get("content", [])
                if c.get("type") == "output_text"
            )
            measured = ProviderUsage(
                token_count(usage.get("input_tokens")),
                token_count(usage.get("output_tokens")),
                token_count(
                    (usage.get("input_tokens_details") or {}).get("cached_tokens")
                ),
            )
            detail["reasoning_tokens"] = token_count(
                (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
            )
        elif self.config.protocol == "anthropic":
            complete = data.get("stop_reason") == "end_turn"
            text = "\n".join(
                c["text"] for c in data.get("content", []) if c.get("type") == "text"
            )
            measured = ProviderUsage(
                token_count(usage.get("input_tokens")),
                token_count(usage.get("output_tokens")),
                token_count(usage.get("cache_read_input_tokens")),
            )
            detail.update(
                input_includes_cache=False,
                cache_write_tokens=token_count(
                    usage.get("cache_creation_input_tokens")
                ),
            )
        else:
            choices = data.get("choices") or []
            choice = choices[0] if len(choices) == 1 else {}
            complete = choice.get("finish_reason") == "stop"
            text = (choice.get("message") or {}).get("content")
            measured = ProviderUsage(
                token_count(usage.get("prompt_tokens")),
                token_count(usage.get("completion_tokens")),
                token_count(
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                ),
            )
            detail["reasoning_tokens"] = token_count(
                (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            )
        diagnostics = {"usage_detail": detail, "reported_model": data.get("model")}
        if not complete or not isinstance(text, str) or not text.strip():
            return ProviderExecution(
                ProviderTerminalKind.FAILED,
                usage=measured,
                failure=ProviderFailure(
                    "API output is incomplete, refused, or missing.",
                    category=FailureCategory.TRANSPORT,
                ),
                diagnostics=diagnostics,
            )
        return ProviderExecution(
            ProviderTerminalKind.COMPLETED,
            (CandidateMaterial(text=text, terminal=True),),
            usage=measured,
            diagnostics=diagnostics,
        )
