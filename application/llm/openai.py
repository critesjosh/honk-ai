import base64
import json
import logging
from typing import Optional

from openai import OpenAI

from application.core.settings import settings
from application.llm.base import BaseLLM
from application.storage.storage_creator import StorageCreator


class OpenAILLM(BaseLLM):
    def __init__(self, api_key=None, user_api_key=None, base_url=None, *args, **kwargs):

        super().__init__(*args, **kwargs)
        self.api_key = api_key or settings.OPENAI_API_KEY or settings.API_KEY
        self.user_api_key = user_api_key

        # Priority: 1) Parameter base_url, 2) Settings OPENAI_BASE_URL, 3) Default
        effective_base_url = None
        if base_url and isinstance(base_url, str) and base_url.strip():
            effective_base_url = base_url
        elif isinstance(settings.OPENAI_BASE_URL, str) and settings.OPENAI_BASE_URL.strip():
            effective_base_url = settings.OPENAI_BASE_URL
        else:
            effective_base_url = "https://api.openai.com/v1"

        self.client = OpenAI(api_key=self.api_key, base_url=effective_base_url)
        self.storage = StorageCreator.get_storage()

    def _clean_messages_openai(self, messages):
        cleaned_messages = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")

            if role == "model":
                role = "assistant"

            # Standard format: assistant message with tool_calls (passthrough)
            tool_calls = message.get("tool_calls")
            if tool_calls and role == "assistant":
                cleaned_tcs = []
                for tc in tool_calls:
                    func = tc.get("function", {})
                    args = func.get("arguments", "{}")
                    if isinstance(args, dict):
                        args = json.dumps(self._remove_null_values(args))
                    elif isinstance(args, str):
                        try:
                            parsed = json.loads(args)
                            args = json.dumps(self._remove_null_values(parsed))
                        except (json.JSONDecodeError, TypeError):
                            pass
                    cleaned_tcs.append(
                        {
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {"name": func.get("name", ""), "arguments": args},
                        }
                    )
                cleaned_messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": cleaned_tcs,
                    }
                )
                continue

            # Standard format: tool message with tool_call_id (passthrough)
            tool_call_id = message.get("tool_call_id")
            if role == "tool" and tool_call_id is not None:
                cleaned_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": content if isinstance(content, str) else json.dumps(content),
                    }
                )
                continue

            if role and content is not None:
                if isinstance(content, str):
                    cleaned_messages.append({"role": role, "content": content})
                elif isinstance(content, list):
                    content_parts = []
                    for item in content:
                        # Legacy format support: function_call / function_response
                        if "function_call" in item:
                            args = item["function_call"]["args"]
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except (json.JSONDecodeError, TypeError):
                                    pass
                            cleaned_args = self._remove_null_values(args)
                            tool_call = {
                                "id": item["function_call"]["call_id"],
                                "type": "function",
                                "function": {
                                    "name": item["function_call"]["name"],
                                    "arguments": json.dumps(cleaned_args),
                                },
                            }
                            cleaned_messages.append(
                                {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [tool_call],
                                }
                            )
                        elif "function_response" in item:
                            cleaned_messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": item["function_response"]["call_id"],
                                    "content": json.dumps(item["function_response"]["response"]["result"]),
                                }
                            )
                        elif isinstance(item, dict):
                            if "type" in item and item["type"] == "text" and "text" in item:
                                content_parts.append(item)
                            elif "type" in item and item["type"] == "file" and "file" in item:
                                content_parts.append(item)
                            elif "type" in item and item["type"] == "image_url" and "image_url" in item:
                                content_parts.append(item)
                            elif "text" in item and "type" not in item:
                                content_parts.append({"type": "text", "text": item["text"]})
                    if content_parts:
                        cleaned_messages.append({"role": role, "content": content_parts})
                else:
                    raise ValueError(f"Unexpected content type: {type(content)}")
        return cleaned_messages

    @staticmethod
    def _normalize_reasoning_value(value):
        """Normalize reasoning payloads from OpenAI-compatible stream chunks."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(OpenAILLM._normalize_reasoning_value(item) for item in value)
        if isinstance(value, dict):
            for key in ("text", "content", "value", "reasoning_content", "reasoning"):
                normalized = OpenAILLM._normalize_reasoning_value(value.get(key))
                if normalized:
                    return normalized
            return ""

        for attr in ("text", "content", "value"):
            if hasattr(value, attr):
                normalized = OpenAILLM._normalize_reasoning_value(getattr(value, attr))
                if normalized:
                    return normalized
        return ""

    @classmethod
    def _extract_reasoning_text(cls, delta):
        """Extract reasoning/thinking tokens from OpenAI-compatible delta chunks."""
        if delta is None:
            return ""

        for key in (
            "reasoning_content",
            "reasoning",
            "thinking",
            "thinking_content",
        ):
            value = getattr(delta, key, None)
            if value is None and isinstance(delta, dict):
                value = delta.get(key)
            normalized = cls._normalize_reasoning_value(value)
            if normalized:
                return normalized
        return ""

    def _raw_gen(
        self,
        baseself,
        model,
        messages,
        stream=False,
        tools=None,
        engine=settings.AZURE_DEPLOYMENT_NAME,
        response_format=None,
        **kwargs,
    ):
        messages = self._clean_messages_openai(messages)
        # Content redacted: messages carry user prompts/history/snippets and
        # persist in container logs at rest. Log shape only. See
        # PLAN-content-encryption.md §3D.
        logging.debug("Prepared %d messages for the LLM call", len(messages))

        # Convert max_tokens to max_completion_tokens for newer models
        if "max_tokens" in kwargs:
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")

        request_params = {
            "model": model,
            "messages": messages,
            "stream": stream,
            **kwargs,
        }

        if tools:
            request_params["tools"] = tools
        if response_format:
            request_params["response_format"] = response_format
        response = self.client.chat.completions.create(**request_params)
        # Content redacted (response body is user-facing answer text). §3D.
        logging.debug("Received OpenAI completion")
        if tools:
            return response.choices[0]
        else:
            return response.choices[0].message.content

    def _raw_gen_stream(
        self,
        baseself,
        model,
        messages,
        stream=True,
        tools=None,
        engine=settings.AZURE_DEPLOYMENT_NAME,
        response_format=None,
        **kwargs,
    ):
        messages = self._clean_messages_openai(messages)
        # Content redacted: messages carry user prompts/history/snippets and
        # persist in container logs at rest. Log shape only. See
        # PLAN-content-encryption.md §3D.
        logging.debug("Prepared %d messages for the LLM call", len(messages))

        # Convert max_tokens to max_completion_tokens for newer models
        if "max_tokens" in kwargs:
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")

        request_params = {
            "model": model,
            "messages": messages,
            "stream": stream,
            **kwargs,
        }

        if tools:
            request_params["tools"] = tools
        if response_format:
            request_params["response_format"] = response_format
        response = self.client.chat.completions.create(**request_params)

        # Empty-response telemetry. Some providers (e.g. OpenRouter →
        # x-ai/grok-4.1-fast under safety filtering) return a 200 with
        # zero content deltas and the OpenAI SDK does NOT raise — the
        # stream just terminates. Without this we save an empty
        # ``response`` row and have no signal as to why. Capture the
        # final finish_reason + any provider error surfaced on the
        # stream lines so the warn log below is actionable.
        had_content = False
        last_finish_reason: Optional[str] = None
        provider_error: Optional[dict] = None

        try:
            for line in response:
                # Content redacted: stream chunks carry the answer text and
                # persist in container logs at rest. See §3D.
                logging.debug("OpenAI stream chunk received")

                # OpenRouter wraps upstream errors as an ``error`` field
                # on a streamed line. The OpenAI SDK preserves it as a
                # dynamic attr. Capture the first one we see — later
                # lines tend to be the same payload. Use ``is not None``
                # rather than truthiness so an empty-dict error payload
                # (rare but legal) still surfaces.
                line_error = getattr(line, "error", None)
                if line_error is not None and provider_error is None:
                    if isinstance(line_error, dict):
                        provider_error = line_error
                    elif hasattr(line_error, "model_dump"):
                        # Pydantic v2 models (OpenAI SDK objects).
                        try:
                            provider_error = line_error.model_dump(exclude_none=True)
                        except Exception:  # noqa: BLE001 — log-only fallback
                            provider_error = {"repr": repr(line_error)}
                    else:
                        provider_error = getattr(line_error, "__dict__", {"repr": repr(line_error)})

                if not getattr(line, "choices", None):
                    continue

                choice = line.choices[0]
                # finish_reason lands on the final delta; keep the last
                # non-None value so the post-loop log has something even
                # if a later chunk arrived without one.
                fr = getattr(choice, "finish_reason", None)
                if fr is not None:
                    last_finish_reason = fr

                delta = getattr(choice, "delta", None)
                reasoning_text = self._extract_reasoning_text(delta)
                if reasoning_text:
                    yield {"type": "thought", "thought": reasoning_text}

                content = getattr(delta, "content", None)
                if isinstance(content, str) and content:
                    had_content = True
                    yield content
                    continue

                has_tool_calls = bool(getattr(delta, "tool_calls", None))

                # Yield non-content chunks only when needed for tool-call
                # handling. Use the per-chunk ``fr`` rather than the
                # sticky ``last_finish_reason`` so a stream that emits
                # extra chunks after a prior ``tool_calls`` finish_reason
                # doesn't keep getting yielded as tool-call choices.
                if has_tool_calls or fr == "tool_calls":
                    yield choice
        finally:
            if hasattr(response, "close"):
                response.close()
            # An empty content stream with a non-``stop`` finish_reason
            # — or with any provider_error payload at all — is the
            # failure mode the angry-operator session on 2026-05-10 hit
            # (three turns silently saved as ``response=''``). Surface
            # it so operators can grep for ``llm.empty_response`` and
            # so message_metadata-level follow-up work has a hook.
            empty_with_signal = not had_content and (
                last_finish_reason not in (None, "stop", "tool_calls") or provider_error is not None
            )
            if empty_with_signal:
                logging.warning(
                    "llm.empty_response model=%s finish_reason=%s provider_error=%s",
                    model,
                    last_finish_reason,
                    provider_error,
                )

    def _supports_tools(self):
        return True

    def _supports_structured_output(self):
        return True

    def prepare_structured_output_format(self, json_schema):
        if not json_schema:
            return None
        try:

            def add_additional_properties_false(schema_obj):
                if isinstance(schema_obj, dict):
                    schema_copy = schema_obj.copy()

                    if schema_copy.get("type") == "object":
                        schema_copy["additionalProperties"] = False
                        # Ensure 'required' includes all properties for OpenAI strict mode

                        if "properties" in schema_copy:
                            schema_copy["required"] = list(schema_copy["properties"].keys())
                    for key, value in schema_copy.items():
                        if key == "properties" and isinstance(value, dict):
                            schema_copy[key] = {
                                prop_name: add_additional_properties_false(prop_schema)
                                for prop_name, prop_schema in value.items()
                            }
                        elif key == "items" and isinstance(value, dict):
                            schema_copy[key] = add_additional_properties_false(value)
                        elif key in ["anyOf", "oneOf", "allOf"] and isinstance(value, list):
                            schema_copy[key] = [add_additional_properties_false(sub_schema) for sub_schema in value]
                    return schema_copy
                return schema_obj

            processed_schema = add_additional_properties_false(json_schema)

            result = {
                "type": "json_schema",
                "json_schema": {
                    "name": processed_schema.get("name", "response"),
                    "description": processed_schema.get("description", "Structured response"),
                    "schema": processed_schema,
                    "strict": True,
                },
            }

            return result
        except Exception as e:
            logging.error(f"Error preparing structured output format: {e}")
            return None

    def get_supported_attachment_types(self):
        """
        Return a list of MIME types supported by OpenAI for file uploads.

        This reads from the model config to ensure consistency.
        If no model config found, falls back to images only (safest default).

        Returns:
            list: List of supported MIME types
        """
        from application.core.model_configs import OPENAI_ATTACHMENTS

        return OPENAI_ATTACHMENTS

    def prepare_messages_with_attachments(self, messages, attachments=None):
        """
        Process attachments using OpenAI's file API for more efficient handling.

        Args:
            messages (list): List of message dictionaries.
            attachments (list): List of attachment dictionaries with content and metadata.

        Returns:
            list: Messages formatted with file references for OpenAI API.
        """
        if not attachments:
            return messages
        prepared_messages = messages.copy()

        # Find the user message to attach file_id to the last one

        user_message_index = None
        for i in range(len(prepared_messages) - 1, -1, -1):
            if prepared_messages[i].get("role") == "user":
                user_message_index = i
                break
        if user_message_index is None:
            user_message = {"role": "user", "content": []}
            prepared_messages.append(user_message)
            user_message_index = len(prepared_messages) - 1
        if isinstance(prepared_messages[user_message_index].get("content"), str):
            text_content = prepared_messages[user_message_index]["content"]
            prepared_messages[user_message_index]["content"] = [{"type": "text", "text": text_content}]
        elif not isinstance(prepared_messages[user_message_index].get("content"), list):
            prepared_messages[user_message_index]["content"] = []
        for attachment in attachments:
            mime_type = attachment.get("mime_type")
            logging.info(
                f"Processing attachment with mime_type: {mime_type}, has_data: {'data' in attachment}, has_path: {'path' in attachment}"
            )

            if mime_type and mime_type.startswith("image/"):
                try:
                    # Check if this is a pre-converted image (from PDF-to-image conversion)
                    if "data" in attachment:
                        base64_image = attachment["data"]
                    else:
                        base64_image = self._get_base64_image(attachment)

                    prepared_messages[user_message_index]["content"].append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{base64_image}"},
                        }
                    )

                except Exception as e:
                    logging.error(f"Error processing image attachment: {e}", exc_info=True)
                    if "content" in attachment:
                        prepared_messages[user_message_index]["content"].append(
                            {
                                "type": "text",
                                "text": f"[Image could not be processed: {attachment.get('path', 'unknown')}]",
                            }
                        )
            # Handle PDFs using the file API

            elif mime_type == "application/pdf":
                logging.info(f"Attempting to upload PDF to OpenAI: {attachment.get('path', 'unknown')}")
                try:
                    file_id = self._upload_file_to_openai(attachment)
                    prepared_messages[user_message_index]["content"].append(
                        {"type": "file", "file": {"file_id": file_id}}
                    )
                except Exception as e:
                    logging.error(f"Error uploading PDF to OpenAI: {e}", exc_info=True)
                    if "content" in attachment:
                        prepared_messages[user_message_index]["content"].append(
                            {
                                "type": "text",
                                "text": f"File content:\n\n{attachment['content']}",
                            }
                        )
            else:
                logging.warning(f"Unsupported attachment type in OpenAI provider: {mime_type}")
        return prepared_messages

    def _get_base64_image(self, attachment):
        """
        Convert an image file to base64 encoding.

        Args:
            attachment (dict): Attachment dictionary with path and metadata.

        Returns:
            str: Base64-encoded image data.
        """
        file_path = attachment.get("path")
        if not file_path:
            raise ValueError("No file path provided in attachment")
        try:
            with self.storage.get_file(file_path) as image_file:
                return base64.b64encode(image_file.read()).decode("utf-8")
        except FileNotFoundError:
            raise FileNotFoundError(f"File not found: {file_path}")

    def _upload_file_to_openai(self, attachment):
        """
        Upload a file to OpenAI and return the file_id.

        Args:
            attachment (dict): Attachment dictionary with path and metadata.
                Expected keys:
                - path: Path to the file
                - id: Optional MongoDB ID for caching

        Returns:
            str: OpenAI file_id for the uploaded file.
        """
        import logging

        if "openai_file_id" in attachment:
            return attachment["openai_file_id"]
        file_path = attachment.get("path")

        if not self.storage.file_exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        try:
            file_id = self.storage.process_file(
                file_path,
                lambda local_path, **kwargs: (
                    self.client.files.create(file=open(local_path, "rb"), purpose="assistants").id
                ),
            )

            # Cache the OpenAI file id on the attachment row so we don't
            # re-upload the same blob on the next LLM call. Prefer the PG
            # UUID (``id``) when present; fall back to the legacy Mongo
            # ObjectId string (``_id``). Opened per-write — this runs
            # inside the hot LLM path, so we don't want a long-lived
            # session wrapping the generator.
            attachment_id = attachment.get("id") or attachment.get("_id")
            if attachment_id:
                user_id = None
                decoded = getattr(self, "decoded_token", None)
                if isinstance(decoded, dict):
                    user_id = decoded.get("sub")
                from application.storage.db.repositories.attachments import (
                    AttachmentsRepository,
                )
                from application.storage.db.session import db_session

                try:
                    with db_session() as conn:
                        AttachmentsRepository(conn).update_any(
                            str(attachment_id),
                            user_id,
                            {"openai_file_id": file_id},
                        )
                except Exception as cache_err:
                    logging.warning(f"Failed to cache openai_file_id on attachment {attachment_id}: {cache_err}")
            return file_id
        except Exception as e:
            logging.error(f"Error uploading file to OpenAI: {e}", exc_info=True)
            raise


class AzureOpenAILLM(OpenAILLM):
    def __init__(self, api_key, user_api_key, *args, **kwargs):

        super().__init__(api_key)
        self.api_base = (settings.OPENAI_API_BASE,)
        self.api_version = (settings.OPENAI_API_VERSION,)
        self.deployment_name = (settings.AZURE_DEPLOYMENT_NAME,)
        from openai import AzureOpenAI

        self.client = AzureOpenAI(
            api_key=api_key,
            api_version=settings.OPENAI_API_VERSION,
            azure_endpoint=settings.OPENAI_API_BASE,
        )
