"""The typed exception-response hierarchy and its code mapping."""

from __future__ import annotations

import pytest

from modbus_connection import (
    BlockReadError,
    ExceptionCode,
    GatewayTargetError,
    IllegalDataAddressError,
    IllegalFunctionError,
    ModbusExceptionError,
    ReadBlock,
    ServerDeviceBusyError,
)
from modbus_connection.exceptions import _CODED_ERRORS


def test_from_code_builds_the_matching_subclass() -> None:
    err = ModbusExceptionError.from_code(2)
    assert type(err) is IllegalDataAddressError
    assert err.exception_code == 2
    assert err.exception_code is ExceptionCode.ILLEGAL_DATA_ADDRESS


def test_every_standard_code_has_a_subclass() -> None:
    for code in ExceptionCode:
        err = ModbusExceptionError.from_code(code)
        assert type(err) is not ModbusExceptionError, code
        assert err.exception_code is code


def test_from_code_falls_back_to_the_base_for_unknown_codes() -> None:
    for code in (0, 7, 0x55, None):
        err = ModbusExceptionError.from_code(code)
        assert type(err) is ModbusExceptionError
        assert err.exception_code == code


def test_coded_subclasses_construct_without_arguments() -> None:
    # The mock-arming case: fail_read(0, IllegalDataAddressError()).
    assert IllegalDataAddressError().exception_code == 2
    assert ServerDeviceBusyError().exception_code == 6
    assert GatewayTargetError().exception_code == 0x0B


def test_base_class_normalizes_known_codes_to_the_enum() -> None:
    assert ModbusExceptionError(2).exception_code is ExceptionCode.ILLEGAL_DATA_ADDRESS
    assert ModbusExceptionError(0x55).exception_code == 0x55  # unknown stays int
    assert ModbusExceptionError(None).exception_code is None


def test_exception_code_comparisons_keep_working() -> None:
    # The pre-enum idiom: err.exception_code == 2.
    assert IllegalDataAddressError().exception_code == 2
    assert ModbusExceptionError.from_code(1).exception_code == 1


def test_subclasses_are_catchable_as_the_base() -> None:
    with pytest.raises(ModbusExceptionError):
        raise IllegalFunctionError()


def test_block_read_error_still_carries_its_code() -> None:
    err = BlockReadError("holding", 100, 4, 2)
    assert isinstance(err, ModbusExceptionError)
    assert err.exception_code is ExceptionCode.ILLEGAL_DATA_ADDRESS
    assert (err.space, err.address, err.count) == ("holding", 100, 4)


def test_coded_registry_covers_every_enum_member() -> None:
    assert set(_CODED_ERRORS) == set(ExceptionCode)


def test_typed_errors_are_catchable_as_block_read_error() -> None:
    # The deprecated catch target keeps working: the chain runs through it.
    with pytest.raises(BlockReadError):
        raise ModbusExceptionError.from_code(2, block=ReadBlock("holding", 100, 4))


def test_a_planner_raise_carries_both_answers() -> None:
    # from_code(..., block=) is the planner's raise: why on the class, where
    # on the block — and the legacy attributes still read through.
    err = ModbusExceptionError.from_code(2, block=ReadBlock("holding", 100, 4))
    assert type(err) is IllegalDataAddressError
    assert err.exception_code is ExceptionCode.ILLEGAL_DATA_ADDRESS
    assert err.block == ReadBlock("holding", 100, 4)
    assert (err.space, err.address, err.count) == ("holding", 100, 4)


def test_the_legacy_constructor_still_builds_and_catches() -> None:
    err = BlockReadError("holding", 0, 1, 0x55)
    assert type(err) is BlockReadError
    assert err.exception_code == 0x55
    assert err.block == ReadBlock("holding", 0, 1)


def test_raw_request_errors_carry_no_block() -> None:
    err = IllegalDataAddressError()
    assert err.block is None
    assert err.space is None  # the legacy attributes read None, not raise


def test_from_code_accepts_a_block() -> None:
    err = ModbusExceptionError.from_code(2, block=ReadBlock("coil", 8, 1))
    assert type(err) is IllegalDataAddressError
    assert err.block == ReadBlock("coil", 8, 1)


def test_the_spec_pattern_reads_the_block_off_the_typed_catch() -> None:
    # The recommended consumer shape: one taxonomy, block as data.
    try:
        raise ModbusExceptionError.from_code(2, block=ReadBlock("holding", 100, 4))
    except IllegalDataAddressError as err:
        assert err.block is not None
        assert (err.block.space, err.block.address, err.block.count) == (
            "holding",
            100,
            4,
        )
