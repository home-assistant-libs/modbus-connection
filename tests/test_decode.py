"""Tests for the pure register-decoding helpers."""

from __future__ import annotations

import ipaddress
import math
import struct

import pytest

from modbus_connection.decode import (
    combine_words,
    decode_eui48,
    decode_float32,
    decode_float64,
    decode_int,
    decode_int16,
    decode_int32,
    decode_int64,
    decode_ipaddr,
    decode_ipv6addr,
    decode_string,
    decode_uint16,
    decode_uint32,
    decode_uint64,
)


def test_combine_words_word_order() -> None:
    assert combine_words([0x0001, 0x86A0]) == 100000
    assert combine_words([0x86A0, 0x0001], word_order="little") == 100000


def test_unsigned_and_signed_16() -> None:
    assert decode_uint16([0xFFFF]) == 65535
    assert decode_int16([0xFFFF]) == -1
    assert decode_int16([0x8000]) == -32768


def test_32_and_64_bit() -> None:
    assert decode_uint32([0x0001, 0x86A0]) == 100000
    assert decode_int32([0xFFFF, 0xFFFF]) == -1
    assert decode_uint64([0, 0, 0, 5]) == 5
    assert decode_int64([0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF]) == -1


def test_decode_int_generic_matches_named() -> None:
    words = [0xFFFF, 0xFFFF]
    assert decode_int(words, signed=True) == -1
    assert decode_int(words, signed=False) == 0xFFFFFFFF


def test_float32_and_float64() -> None:
    words32 = list(struct.unpack(">HH", struct.pack(">f", 12.5)))
    assert decode_float32(words32) == pytest.approx(12.5)
    words64 = list(struct.unpack(">HHHH", struct.pack(">d", -3.5)))
    assert decode_float64(words64) == pytest.approx(-3.5)


def test_float_word_order_little() -> None:
    words = list(struct.unpack(">HH", struct.pack(">f", 7.25)))
    assert decode_float32(list(reversed(words)), word_order="little") == pytest.approx(
        7.25
    )


def test_string_strips_null_padding() -> None:
    assert decode_string([0x4142, 0x4344, 0x0000]) == "ABCD"


def test_address_formats() -> None:
    assert decode_ipaddr([0xC0A8, 0x0001]) == ipaddress.IPv4Address("192.168.0.1")
    assert decode_ipv6addr([0x2001, 0xDB8, 0, 0, 0, 0, 0, 1]) == ipaddress.IPv6Address(
        "2001:db8::1"
    )
    assert decode_eui48([0x0011, 0x2233, 0x4455]) == "00:11:22:33:44:55"


def test_float_nan_is_a_float() -> None:
    nan_words = list(struct.unpack(">HH", struct.pack(">f", math.nan)))
    assert math.isnan(decode_float32(nan_words))
