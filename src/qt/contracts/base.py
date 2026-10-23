"""Strict immutable contracts and stable, content-addressed serialization.

Financial values accept strings or ``Decimal`` objects, never binary floats.
Timestamps are integer nanoseconds since the Unix epoch. Mutable JSON supplied
by callers is copied recursively before it becomes part of a signed contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, model_validator
from pydantic_core import core_schema


def decimal_string(value: Decimal) -> str:
    """Canonical decimal text without exponent, rounding or negative zero."""
    if not value.is_finite():
        raise ValueError("financial values must be finite")
    if value == 0:
        return "0"
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str) and len(value) <= 128 and re.fullmatch(
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value
    ):
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("invalid decimal string") from exc
    else:
        raise ValueError("financial values require decimal strings or Decimal, not numbers")
    exponent = parsed.as_tuple().exponent
    if not parsed.is_finite() or not isinstance(exponent, int) or abs(exponent) > 1000:
        raise ValueError("financial values must be finite with a bounded exponent")
    integer, _, fraction = decimal_string(parsed).lstrip("-").partition(".")
    if len(integer.lstrip("0")) > 20 or len(fraction) > 18:
        raise ValueError("financial values must fit NUMERIC(38,18) without rounding")
    return parsed


DecimalString = Annotated[
    Decimal,
    BeforeValidator(_decimal),
    PlainSerializer(decimal_string, return_type=str, when_used="always"),
]
PositiveDecimal = Annotated[DecimalString, Field(gt=Decimal("0"))]
NonNegativeDecimal = Annotated[DecimalString, Field(ge=Decimal("0"))]
Probability = Annotated[DecimalString, Field(ge=Decimal("0"), le=Decimal("1"))]
TimestampNS = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
NonEmptyStr = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def _freeze(value: object) -> object:
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, Decimal):
        return decimal_string(value)
    raise ValueError("contract JSON must contain finite JSON primitives only")


def _thaw(value: object) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    if isinstance(value, Decimal):
        return decimal_string(value)
    return value


class FrozenDict(Mapping[str, object]):
    """Deeply immutable JSON object with a Pydantic JSON-object wire shape."""

    __slots__ = ("_values",)
    _values: Mapping[str, object]

    def __init__(self, values: Mapping[str, object] | None = None) -> None:
        source = {} if values is None else values
        if not isinstance(source, Mapping) or any(type(key) is not str for key in source):
            raise ValueError("JSON objects require string keys")
        object.__setattr__(self, "_values", MappingProxyType({
            key: _freeze(item) for key, item in source.items()
        }))

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("contract JSON is immutable")

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"FrozenDict({dict(self._values)!r})"

    @classmethod
    def __get_pydantic_core_schema__(cls, source: object, handler: object) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls._validate,
            serialization=core_schema.plain_serializer_function_ser_schema(_thaw),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: object, handler: object) -> dict[str, object]:
        # Keep compatibility with the repository's minimum Pydantic 2.5, whose
        # pydantic-core predates json_schema_input_schema on plain validators.
        return {"type": "object", "additionalProperties": {}}

    @classmethod
    def _validate(cls, value: object) -> FrozenDict:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("expected a JSON object")
        return cls(value)


def canonical_json(value: object) -> str:
    """Stable UTF-8 JSON used for identities, idempotency and audit signatures."""
    return json.dumps(_thaw(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True, allow_inf_nan=False,
    )

    schema_version: Literal["2.0"] = "2.0"

    @model_validator(mode="before")
    @classmethod
    def immutable_arrays(cls, value: Any) -> Any:
        # FastAPI validates already-decoded JSON. Accept JSON arrays while
        # retaining strict scalar validation and immutable in-memory tuples.
        def convert(item: Any) -> Any:
            if type(item) is list:
                return tuple(convert(element) for element in item)
            if type(item) is dict:
                return {key: convert(element) for key, element in item.items()}
            return item
        return convert(value)

    def canonical_json(self) -> str:
        return canonical_json(self)

    def content_hash(self) -> str:
        return content_hash(self)
