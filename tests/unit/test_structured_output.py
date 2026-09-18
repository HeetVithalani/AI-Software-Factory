"""
Unit tests for factory.llm.structured_output
"""

import json
import pytest
from pydantic import BaseModel, Field

from factory.llm.structured_output import (
    extract_json_from_text,
    parse_structured_response,
    parse_structured_data,
    StructuredOutputError,
)


# ---------------------------------------------------------------------------
# Simple test schemas
# ---------------------------------------------------------------------------

class SimpleModel(BaseModel):
    name: str
    value: int


class NestedModel(BaseModel):
    summary: str
    completed: bool = False
    items: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# extract_json_from_text
# ---------------------------------------------------------------------------

class TestExtractJsonFromText:
    def test_pure_json_object(self):
        text = '{"name": "Alice", "value": 42}'
        result = extract_json_from_text(text)
        assert json.loads(result) == {"name": "Alice", "value": 42}

    def test_pure_json_array(self):
        text = '[1, 2, 3]'
        result = extract_json_from_text(text)
        assert json.loads(result) == [1, 2, 3]

    def test_json_in_markdown_fence(self):
        text = "Here is the result:\n```json\n{\"name\": \"Bob\", \"value\": 7}\n```"
        result = extract_json_from_text(text)
        assert result is not None
        assert json.loads(result)["name"] == "Bob"

    def test_json_in_unlabeled_fence(self):
        text = "```\n{\"name\": \"Charlie\", \"value\": 99}\n```"
        result = extract_json_from_text(text)
        assert result is not None
        data = json.loads(result)
        assert data["value"] == 99

    def test_json_embedded_in_prose(self):
        text = 'The system returned {"name": "Dave", "value": 10} as the result.'
        result = extract_json_from_text(text)
        assert result is not None
        assert json.loads(result)["name"] == "Dave"

    def test_trailing_comma_fixed(self):
        text = '{"name": "Eve", "value": 5,}'
        result = extract_json_from_text(text)
        assert result is not None
        assert json.loads(result)["name"] == "Eve"

    def test_empty_string_returns_none(self):
        assert extract_json_from_text("") is None

    def test_plain_prose_returns_none(self):
        assert extract_json_from_text("No JSON here, just text.") is None

    def test_nested_json(self):
        text = '{"outer": {"inner": 42}}'
        result = extract_json_from_text(text)
        assert json.loads(result)["outer"]["inner"] == 42

    def test_json_with_unicode(self):
        text = '{"name": "Ünïcödé", "value": 1}'
        result = extract_json_from_text(text)
        assert json.loads(result)["name"] == "Ünïcödé"


# ---------------------------------------------------------------------------
# parse_structured_response
# ---------------------------------------------------------------------------

class TestParseStructuredResponse:
    def test_valid_json_string_parsed(self):
        text = '{"name": "Alice", "value": 42}'
        obj = parse_structured_response(text, SimpleModel)
        assert obj.name == "Alice"
        assert obj.value == 42

    def test_json_in_fence_parsed(self):
        text = '```json\n{"name": "Bob", "value": 7}\n```'
        obj = parse_structured_response(text, SimpleModel)
        assert obj.name == "Bob"

    def test_missing_field_raises_structured_error(self):
        text = '{"name": "Alice"}'  # missing "value"
        with pytest.raises(StructuredOutputError, match="validation failed"):
            parse_structured_response(text, SimpleModel)

    def test_wrong_type_raises_structured_error(self):
        text = '{"name": "Alice", "value": "not_an_int"}'
        with pytest.raises(StructuredOutputError):
            parse_structured_response(text, SimpleModel)

    def test_no_json_raises_structured_error(self):
        with pytest.raises(StructuredOutputError, match="No valid JSON"):
            parse_structured_response("Just prose here.", SimpleModel)

    def test_context_appears_in_error(self):
        with pytest.raises(StructuredOutputError) as exc_info:
            parse_structured_response("no json", SimpleModel, context="turn 3")
        assert "turn 3" in str(exc_info.value)

    def test_nested_model(self):
        text = '{"summary": "Done", "completed": true, "items": ["a", "b"]}'
        obj = parse_structured_response(text, NestedModel)
        assert obj.completed is True
        assert obj.items == ["a", "b"]


# ---------------------------------------------------------------------------
# parse_structured_data
# ---------------------------------------------------------------------------

class TestParseStructuredData:
    def test_valid_dict_parsed(self):
        data = {"name": "Alice", "value": 42}
        obj = parse_structured_data(data, SimpleModel)
        assert obj.name == "Alice"

    def test_empty_dict_raises(self):
        with pytest.raises(StructuredOutputError):
            parse_structured_data({}, SimpleModel)

    def test_invalid_dict_raises(self):
        with pytest.raises(StructuredOutputError):
            parse_structured_data({"name": "Alice"}, SimpleModel)


# ---------------------------------------------------------------------------
# StructuredOutputError
# ---------------------------------------------------------------------------

class TestStructuredOutputError:
    def test_carries_raw_content(self):
        err = StructuredOutputError("oops", raw_content="raw stuff")
        assert err.raw_content == "raw stuff"

    def test_is_exception(self):
        with pytest.raises(StructuredOutputError):
            raise StructuredOutputError("test")
