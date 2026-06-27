"""Tests for the pure value-encoding helpers (round-trips against decode)."""

from __future__ import annotations

import pytest

from modbus_connection.decode import (
    decode_float32,
    decode_float64,
    decode_int32,
    decode_string,
    decode_uint32,
)
from modbus_connection.encode import (
    encode_float32,
    encode_float64,
    encode_int,
    encode_int16,
    encode_int32,
    encode_string,
    encode_uint16,
    encode_uint32,
    split_words,
)


def test_split_words() -> None:
    assert split_words(100000, count=2) == [0x0001, 0x86A0]
    assert split_words(100000, count=2, word_order="little") == [0x86A0, 0x0001]


def test_split_words_byte_order() -> None:
    # The four byte arrangements of 0x12345678 (ABCD / CDAB / BADC / DCBA).
    assert split_words(0x12345678, count=2) == [0x1234, 0x5678]
    assert split_words(0x12345678, count=2, word_order="little") == [0x5678, 0x1234]
    assert split_words(0x12345678, count=2, byte_order="little") == [0x3412, 0x7856]
    assert split_words(
        0x12345678, count=2, word_order="little", byte_order="little"
    ) == [0x7856, 0x3412]


def test_byte_order_round_trips() -> None:
    # Every word/byte order round-trips through its matching decode.
    for word_order in ("big", "little"):
        for byte_order in ("big", "little"):
            words = encode_uint32(100000, word_order=word_order, byte_order=byte_order)
            assert (
                decode_uint32(words, word_order=word_order, byte_order=byte_order)
                == 100000
            )
    assert (
        decode_string(
            encode_string("ABCD", length=3, byte_order="little"), byte_order="little"
        )
        == "ABCD"
    )
    assert decode_float32(
        encode_float32(-7.25, byte_order="little"), byte_order="little"
    ) == pytest.approx(-7.25)


def test_int_round_trips() -> None:
    assert encode_uint16(4321) == [4321]
    assert encode_int16(-1) == [0xFFFF]
    assert decode_uint32(encode_uint32(100000)) == 100000
    assert decode_int32(encode_int32(-12345)) == -12345


def test_int_word_order() -> None:
    assert (
        decode_uint32(encode_uint32(100000, word_order="little"), word_order="little")
        == 100000
    )


def test_negative_two_complement() -> None:
    assert encode_int(-1, count=1) == [0xFFFF]
    assert encode_int(-1, count=4) == [0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF]


def test_encode_int_rejects_out_of_range() -> None:
    # Accepts the full signed and unsigned range for the width...
    assert encode_int(0xFFFF, count=1) == [0xFFFF]
    assert encode_int(-0x8000, count=1) == [0x8000]
    # ...but raises rather than silently truncating onto the wire.
    for value, count in [(0x10000, 1), (70000, 1), (-0x8001, 1), (1 << 32, 2)]:
        with pytest.raises(OverflowError):
            encode_int(value, count=count)


def test_float_round_trips() -> None:
    assert decode_float32(encode_float32(3.5)) == pytest.approx(3.5)
    assert decode_float64(encode_float64(-7.25)) == pytest.approx(-7.25)
    assert decode_float32(
        encode_float32(-7.25, word_order="little"), word_order="little"
    ) == pytest.approx(-7.25)


def test_string_pads_to_length() -> None:
    words = encode_string("ABCD", length=3)
    assert len(words) == 3
    assert decode_string(words) == "ABCD"
