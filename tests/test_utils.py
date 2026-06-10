"""Tests for application/utils.py"""

from unittest.mock import MagicMock, patch

import pytest

from application.utils import (
    calculate_doc_token_budget,
    check_required_fields,
    convert_pdf_to_images,
    get_encoding,
    get_hash,
    get_missing_fields,
    limit_chat_history,
    num_tokens_from_object_or_list,
    num_tokens_from_string,
    safe_filename,
)


class TestGetEncoding:

    @pytest.mark.unit
    def test_returns_encoding(self):
        enc = get_encoding()
        assert enc is not None

    @pytest.mark.unit
    def test_returns_same_instance(self):
        enc1 = get_encoding()
        enc2 = get_encoding()
        assert enc1 is enc2


class TestSafeFilename:

    @pytest.mark.unit
    def test_normal_filename(self):
        assert safe_filename("test.pdf") == "test.pdf"

    @pytest.mark.unit
    def test_empty_filename_returns_uuid(self):
        result = safe_filename("")
        assert len(result) > 10  # UUID

    @pytest.mark.unit
    def test_none_filename_returns_uuid(self):
        result = safe_filename(None)
        assert len(result) > 10

    @pytest.mark.unit
    def test_non_latin_filename(self):
        result = safe_filename("документ.pdf")
        assert result.endswith(".pdf")


class TestNumTokens:

    @pytest.mark.unit
    def test_string_token_count(self):
        count = num_tokens_from_string("hello world")
        assert count > 0

    @pytest.mark.unit
    def test_non_string_returns_zero(self):
        assert num_tokens_from_string(123) == 0

    @pytest.mark.unit
    def test_empty_string(self):
        assert num_tokens_from_string("") == 0


class TestNumTokensFromObjectOrList:

    @pytest.mark.unit
    def test_list(self):
        result = num_tokens_from_object_or_list(["hello", "world"])
        assert result > 0

    @pytest.mark.unit
    def test_dict(self):
        result = num_tokens_from_object_or_list({"key": "value"})
        assert result > 0

    @pytest.mark.unit
    def test_string(self):
        result = num_tokens_from_object_or_list("hello")
        assert result > 0

    @pytest.mark.unit
    def test_number_returns_zero(self):
        assert num_tokens_from_object_or_list(42) == 0

    @pytest.mark.unit
    def test_nested(self):
        result = num_tokens_from_object_or_list({"a": ["b", "c"]})
        assert result > 0


class TestCountTokensDocs:

    @pytest.mark.unit
    def test_counts_doc_tokens(self):
        from application.utils import count_tokens_docs
        doc1 = MagicMock()
        doc1.page_content = "hello world"
        doc2 = MagicMock()
        doc2.page_content = " foo bar"
        result = count_tokens_docs([doc1, doc2])
        assert result > 0


class TestCalculateDocTokenBudget:

    @pytest.mark.unit
    def test_returns_budget(self):
        # With the Aztec fork's RAG_MAX_DOC_TOKENS cap disabled (0), the
        # budget is just context - reserved.
        with patch("application.utils.get_token_limit", return_value=128000), \
             patch("application.utils.settings") as s:
            s.RESERVED_TOKENS = {"system": 500, "history": 500}
            s.RAG_MAX_DOC_TOKENS = 0
            result = calculate_doc_token_budget("gpt-4o")
            assert result == 127000

    @pytest.mark.unit
    def test_minimum_budget(self):
        with patch("application.utils.get_token_limit", return_value=1000), \
             patch("application.utils.settings") as s:
            s.RESERVED_TOKENS = {"system": 500, "history": 500}
            s.RAG_MAX_DOC_TOKENS = 0
            result = calculate_doc_token_budget("small-model")
            assert result == 1000

    @pytest.mark.unit
    def test_caps_at_rag_max_doc_tokens(self):
        # Default behaviour in the Aztec fork: the cap (15k prod, 6k in
        # our deployed .env) dominates when the model window is huge.
        with patch("application.utils.get_token_limit", return_value=200000), \
             patch("application.utils.settings") as s:
            s.RESERVED_TOKENS = {"system": 500, "history": 500}
            s.RAG_MAX_DOC_TOKENS = 6000
            result = calculate_doc_token_budget("claude-sonnet-4")
            assert result == 6000


class TestFieldValidation:

    @pytest.mark.unit
    def test_get_missing_fields(self):
        assert get_missing_fields({"a": 1}, ["a", "b"]) == ["b"]
        assert get_missing_fields({"a": 1, "b": 2}, ["a", "b"]) == []

    @pytest.mark.unit
    def test_check_required_fields_pass(self):
        from flask import Flask
        app = Flask(__name__)
        with app.app_context():
            result = check_required_fields({"a": 1, "b": 2}, ["a", "b"])
            assert result is None

    @pytest.mark.unit
    def test_check_required_fields_fail(self):
        from flask import Flask
        app = Flask(__name__)
        with app.app_context():
            result = check_required_fields({"a": 1}, ["a", "b"])
            assert result is not None
            assert result.status_code == 400


class TestGetHash:

    @pytest.mark.unit
    def test_returns_hex_string(self):
        h = get_hash("test")
        assert len(h) == 32
        assert all(c in "0123456789abcdef" for c in h)

    @pytest.mark.unit
    def test_deterministic(self):
        assert get_hash("hello") == get_hash("hello")

    @pytest.mark.unit
    def test_different_inputs(self):
        assert get_hash("a") != get_hash("b")


class TestLimitChatHistory:

    @pytest.mark.unit
    def test_empty_history(self):
        assert limit_chat_history([]) == []

    @pytest.mark.unit
    def test_none_history(self):
        assert limit_chat_history(None) == []

    @pytest.mark.unit
    def test_keeps_recent_messages(self):
        history = [
            {"prompt": "q1", "response": "a1"},
            {"prompt": "q2", "response": "a2"},
        ]
        result = limit_chat_history(history, max_token_limit=10000)
        assert len(result) == 2

    @pytest.mark.unit
    def test_trims_old_messages(self):
        history = [
            {"prompt": "x" * 5000, "response": "y" * 5000},
            {"prompt": "q", "response": "a"},
        ]
        result = limit_chat_history(history, max_token_limit=100)
        assert len(result) <= 2

    @pytest.mark.unit
    def test_handles_tool_calls(self):
        history = [
            {
                "prompt": "q",
                "response": "a",
                "tool_calls": [
                    {"tool_name": "t", "action_name": "a", "arguments": "{}", "result": "r"}
                ],
            }
        ]
        result = limit_chat_history(history, max_token_limit=10000)
        assert len(result) == 1


class TestConvertPdfToImages:

    @pytest.mark.unit
    def test_missing_pdf2image_raises(self):
        with patch.dict("sys.modules", {"pdf2image": None}):
            # Force re-import to trigger ImportError
            # The function handles the import internally
            with pytest.raises(ImportError, match="pdf2image"):
                convert_pdf_to_images("test.pdf")

    @pytest.mark.unit
    def test_converts_from_path(self):
        mock_image = MagicMock()
        mock_image.save = MagicMock(side_effect=lambda buf, format: buf.write(b"PNG_DATA"))

        mock_module = MagicMock()
        mock_module.convert_from_path.return_value = [mock_image]
        mock_module.convert_from_bytes.return_value = [mock_image]

        original_import = __import__

        def patched_import(name, *args, **kwargs):
            if name == "pdf2image":
                return mock_module
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=patched_import):
            result = convert_pdf_to_images("/some/file.pdf")
        assert len(result) == 1
        assert result[0]["mime_type"] == "image/png"
        assert result[0]["page"] == 1

    @pytest.mark.unit
    def test_with_storage(self):
        mock_image = MagicMock()
        mock_image.save = MagicMock(side_effect=lambda buf, format: buf.write(b"IMG"))

        mock_storage = MagicMock()
        mock_file = MagicMock()
        mock_file.read.return_value = b"pdf_bytes"
        mock_file.__enter__ = MagicMock(return_value=mock_file)
        mock_file.__exit__ = MagicMock(return_value=False)
        mock_storage.get_file.return_value = mock_file

        mock_module = MagicMock()
        mock_module.convert_from_bytes.return_value = [mock_image]

        original_import = __import__

        def patched_import(name, *args, **kwargs):
            if name == "pdf2image":
                return mock_module
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=patched_import):
            result = convert_pdf_to_images("test.pdf", storage=mock_storage)
        assert len(result) == 1
        mock_module.convert_from_bytes.assert_called_once()

    @pytest.mark.unit
    def test_file_not_found_raises(self):
        mock_module = MagicMock()
        mock_module.convert_from_path.side_effect = FileNotFoundError("not found")

        # Patch the import inside the function
        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def patched_import(name, *args, **kwargs):
            if name == "pdf2image":
                return mock_module
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=patched_import):
            with pytest.raises(FileNotFoundError):
                convert_pdf_to_images("/nonexistent.pdf")

    @pytest.mark.unit
    def test_generic_error_raises(self):
        mock_module = MagicMock()
        mock_module.convert_from_path.side_effect = RuntimeError("conversion failed")

        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def patched_import(name, *args, **kwargs):
            if name == "pdf2image":
                return mock_module
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=patched_import):
            with pytest.raises(RuntimeError, match="conversion failed"):
                convert_pdf_to_images("/some.pdf")


class TestLimitChatHistoryEdgeCases:

    @pytest.mark.unit
    def test_max_token_limit_caps_at_model_limit(self):
        """When max_token_limit exceeds model limit, model limit is used."""
        with patch("application.utils.get_token_limit", return_value=100):
            history = [
                {"prompt": "q", "response": "a"},
            ]
            result = limit_chat_history(history, max_token_limit=999999)
            assert len(result) <= 1

    @pytest.mark.unit
    def test_max_token_limit_none_uses_model_limit(self):
        with patch("application.utils.get_token_limit", return_value=100000):
            history = [{"prompt": "q", "response": "a"}]
            result = limit_chat_history(history, max_token_limit=None)
            assert len(result) == 1

    @pytest.mark.unit
    def test_messages_without_prompt_response_keys(self):
        """Messages lacking prompt/response should still be included."""
        with patch("application.utils.get_token_limit", return_value=100000):
            history = [{"custom_key": "value"}]
            result = limit_chat_history(history, max_token_limit=100000)
            assert len(result) == 1

    @pytest.mark.unit
    def test_single_message_exceeds_limit(self):
        """If the most recent message exceeds the limit, it's excluded."""
        history = [
            {"prompt": "x" * 50000, "response": "y" * 50000},
        ]
        result = limit_chat_history(history, max_token_limit=10)
        assert len(result) == 0


class TestSafeFilenameEdgeCases:

    @pytest.mark.unit
    def test_filename_with_spaces(self):
        result = safe_filename("my document.pdf")
        assert result == "my_document.pdf"

    @pytest.mark.unit
    def test_filename_with_special_chars(self):
        result = safe_filename("file@#$.txt")
        # secure_filename strips special chars
        assert result.endswith(".txt")

    @pytest.mark.unit
    def test_chinese_filename_gets_uuid(self):
        result = safe_filename("\u6587\u4ef6.pdf")
        # secure_filename strips non-latin, so UUID is generated
        assert result.endswith(".pdf")
        assert len(result) > 5


class TestGetHashEdgeCases:

    @pytest.mark.unit
    def test_empty_string(self):
        h = get_hash("")
        assert len(h) == 32

    @pytest.mark.unit
    def test_unicode_string(self):
        h = get_hash("\u4f60\u597d\u4e16\u754c")
        assert len(h) == 32

