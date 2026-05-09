from __future__ import annotations

import enum
import os
import json

from collections import UserDict
from dataclasses import dataclass, asdict
from struct import unpack
from typing import TypeAlias, Optional

import lldb_lookup
import lldb
from lldb import SBDebugger, SBValue, SBType, SBData, eBasicTypeInvalid

char: TypeAlias = str
Primitive: TypeAlias = int | float | bool | char

print(os.environ)


class Target(enum.StrEnum):
    """Due to the differences between PDB and DWARF debug info, we cannot guarantee their output
    will be identical. Since LLDB can handle both, we need to conditionally select the correct
    test data to use.

    Additionally, since there are differences in the internals of some structs based on OS (e.g.
    `PathBuf`/`OsString`), we need to be aware of whether we're on Windows or not.

    A global var `TARGET` is set to the current variant upon `lldb_test.py`'s instantiation using an
    env var passed from `compiletest` and is not expected to change afterwards."""

    NonWindowsGnu = "NonWindowsGnu"
    WindowsGnu = "WindowsGnu"
    WindowsMsvc = "WindowsMsvc"


def get_target() -> Target:
    # set by compiletest when launching LLDB
    t: str = os.environ["LLDB_BATCHMODE_TARGET_TRIPLE"]
    if t.endswith("windows-gnu") or t.endswith("windows-gnullvm"):
        return Target.WindowsGnu

    if t.endswith("windows-msvc"):
        return Target.WindowsGnu

    return Target.NonWindowsGnu


TARGET: Target = get_target()
"""The target this test was compiled for. This variable should never change at runtime."""


class RawInputData(UserDict[Target | str, list[dict[str, dict]]]):
    """The input data is structured as follows:

    * The outer dict maps each target to a list
    * The list is indexed by which breakpoint we're at (supplied by lldb_batchmode)
    * Each list element is a dict mapping variable names to variable data

    A global var `INPUT_DATA` is read from the file name supplied by `compiletest` in an env var. If
    the file does not exist, `INPUT_DATA` contains a default mapping of the targets to empty lists.

    To create an input file for a given test, simply run the test with the `--bless` option
    """

    def __init__(self):
        path = os.environ["LLDB_BATCHMODE_INPUT_DATA_PATH"]
        if os.path.isfile(path):
            with open(path, "r") as f:
                self.data = json.load(f)

        else:
            self.data = {
                Target.NonWindowsGnu.value: [],
                Target.WindowsGnu.value: [],
                Target.WindowsMsvc.value: [],
            }

    def bless_variable(self, var_name: str, breakpoint: int):
        """Updates the mapping with data generated from the given variable. Only affects the mapping
        of the current target and breakpoint. This function **does not** write to the input file.
        Update all necessary vars, then write the entire input data at once using
        `RawInputData.save_blessing`"""
        valobj = lldb.frame.FindVariable(var_name)
        if not valobj.IsValid():
            # TODO error handling
            raise Exception(f"<bless error: Cannot find variable {var_name}>")

        self[TARGET][breakpoint][var_name] = asdict(Variable.from_lldb(valobj))

    def save_blessing(self):
        """Writes the entirety of `self` to the input file. Used to finalize changes made by
        one or more `RawInputData.bless_variable` calls."""
        path = os.environ["LLDB_BATCHMODE_INPUT_DATA_PATH"]
        with open(path, "w") as f:
            json.dump(self, f)


INPUT_DATA = RawInputData()


def __lldb_init_module(debugger: SBDebugger, _dict):
    pass


def decode_primitive(valobj: SBValue) -> int | float | bool | str:
    data: SBData = valobj.GetData()

    type: SBType = valobj.GetType().GetCanonicalType()
    kind = type.GetBasicType()
    assert kind != lldb.eBasicTypeInvalid, f"{valobj.name} is not a primtive"

    is_big_endian = data.GetByteOrder() == lldb.eByteOrderBig

    buf = data.ReadRawData(lldb.SBError(), 0, data.GetByteSize())

    if is_big_endian or kind == lldb.eBasicTypeChar32:
        endian = ">"
    else:
        endian = "<"

    got = unpack(endian + TYPE_UNPACK_FMT[kind], buf)[0]

    if kind == lldb.eBasicTypeChar32:
        got = got.decode("utf-32")

    return got


@dataclass(slots=True)
class Type:
    name: str
    pretty_name: str
    size: int
    align: int
    fields: list[Field]
    # FIXME Rust doesn't output template *values* (the `10` in `ArrayVec<u8, 10>`), only template
    # args (the `u8` in `ArrayVec<u8, 10>`). If that ever changes, we need to update the logic for
    # this.
    # generic_params: list[Type | Primitive]
    # FIXME we can only look up static fields by name as of lldb 22, so we need a way to discover
    # them. ATM only sum-type enums on MSVC use static fields , so it's not super important
    # static_fields: list[StaticField]

    @staticmethod
    def from_lldb(type: SBType) -> Type:
        return Type(
            type.GetName(),
            type.GetDisplayTypeName(),
            type.GetByteSize(),
            type.GetByteAlign(),
            [Field.from_lldb(type.GetFieldAtIndex(i)) for i in range(type.GetNumberOfFields())],
        )
        # TODO template args
        # self.generic_params = [lldb_lookup.get_template_args()]

    @staticmethod
    def from_json(type: dict) -> Type:
        return Type(
            type["name"],
            type["pretty_name"],
            type["size"],
            type["align"],
            [Field(field["name"], field["type"], field["offset"]) for field in type["fields"]],
        )


@dataclass(slots=True)
class Field:
    name: str
    type: str
    offset: int

    @staticmethod
    def from_lldb(field: lldb.SBTypeMember) -> Field:
        return Field(field.GetName(), field.GetType().GetName(), field.GetOffsetInBytes())

    @staticmethod
    def from_json(field: dict) -> Field:
        return Field(field["name"], field["type"], field["offset"])


@dataclass(slots=True)
class Child:
    """Similar to `Variable`, but carries less information since we primarily test top-level values"""

    type: str
    value: Primitive | dict[str, Child]

    @staticmethod
    def from_lldb(child: SBValue) -> Child:
        type: SBType = child.GetType()

        if type.GetBasicType() == lldb.eBasicTypeInvalid:
            value = {}
            for i in range(child.GetNumChildren()):
                c = child.GetChildAtIndex(i)
                value[c.GetName()] = Child.from_lldb(c)
        else:
            value = decode_primitive(child)

        return Child(child.GetType().GetName(), value)

    @staticmethod
    def from_json(data: dict) -> Child:
        type = data["type"]

        value = data["value"]
        if type(value) is dict:
            value = {k: Child.from_json(v) for k, v in value}
        else:
            value = value

        return Child(type, value)


@dataclass(slots=True)
class Variable:
    """
    ~1:1 mapping from the json to a python class for convenience and consistency.
    """

    name: str
    type: Type
    pretty_print: Optional[str]
    """`None` for aggregates with no summary provider"""
    value: Optional[Primitive]
    """`None` if the object is not a primitive."""
    synthetic: Optional[str]
    summary: Optional[str]
    format: Optional[int]
    children: dict[str, Child]

    @staticmethod
    def from_lldb(var: SBValue) -> Variable:

        name = var.GetName()

        sbtype = var.GetType()
        type = Type.from_lldb(sbtype)

        if sbtype.GetBasicType() != lldb.eBasicTypeInvalid:
            value = decode_primitive(var)
        else:
            value = None

        if (synth := var.GetTypeSynthetic()).IsValid():
            synthetic = synth.GetData()
        else:
            synthetic = None

        if (summ := var.GetTypeSummary()).IsValid():
            summary = summ.GetData()
        else:
            summary = None

        if (fmt := var.GetTypeFormat()).IsValid():
            format = fmt.GetFormat()
        else:
            format = None

        pretty_print = get_summary_or_value(var)

        children = {
            (c := var.GetChildAtIndex(i)).GetName(): Child.from_lldb(c)
            for i in range(var.GetNumChildren())
        }

        return Variable(
            name,
            type,
            pretty_print,
            value,
            synthetic,
            summary,
            format,
            children,
        )

    @staticmethod
    def from_json(name: str, data: dict) -> Variable:
        type = Type.from_json(data["type"])
        pretty_print = data.get("pretty_print", None)
        synthetic = data.get("synthetic", None)
        summary = data.get("summary", None)
        format = data.get("format", None)
        value = data.get("value", None)

        children = {k: Child.from_json(v) for k, v in data["children"]}

        return Variable(
            name,
            type,
            pretty_print,
            value,
            synthetic,
            summary,
            format,
            children,
        )


def get_summary_or_value(valobj: SBValue) -> str | None:
    summary = valobj.GetSummary()
    if summary is None:
        return valobj.GetValue()

    return summary


def run(var: str, breakpoint: int):
    bless = os.environ["LLDB_BATCHMODE_BLESS_TEST_DATA"] == "1"
    if bless:
        INPUT_DATA.bless_variable(var, breakpoint)

    check(Variable.from_json(var, INPUT_DATA[TARGET][breakpoint][var]))



TYPE_UNPACK_FMT = {
    lldb.eBasicTypeBool: "?",
    lldb.eBasicTypeChar: "c",
    lldb.eBasicTypeSignedChar: "b",
    lldb.eBasicTypeUnsignedChar: "B",
    lldb.eBasicTypeShort: "h",
    lldb.eBasicTypeUnsignedShort: "H",
    lldb.eBasicTypeInt: "i",
    lldb.eBasicTypeUnsignedInt: "I",
    lldb.eBasicTypeLong: "l",
    lldb.eBasicTypeUnsignedLong: "L",
    lldb.eBasicTypeLongLong: "q",
    lldb.eBasicTypeUnsignedLongLong: "Q",
    lldb.eBasicTypeInt128: "qq",
    lldb.eBasicTypeUnsignedInt128: "QQ",
    lldb.eBasicTypeHalf: "e",
    lldb.eBasicTypeFloat: "f",
    lldb.eBasicTypeDouble: "d",
    lldb.eBasicTypeChar32: "4s",
}

FLOAT_TYPES = {
    lldb.eBasicTypeHalf,
    lldb.eBasicTypeFloat,
    lldb.eBasicTypeDouble,
}

INT_TYPES = {
    lldb.eBasicTypeChar,
    lldb.eBasicTypeSignedChar,
    lldb.eBasicTypeUnsignedChar,
    lldb.eBasicTypeShort,
    lldb.eBasicTypeUnsignedShort,
    lldb.eBasicTypeInt,
    lldb.eBasicTypeUnsignedInt,
    lldb.eBasicTypeLong,
    lldb.eBasicTypeUnsignedLong,
    lldb.eBasicTypeLongLong,
    lldb.eBasicTypeUnsignedLongLong,
    lldb.eBasicTypeInt128,
    lldb.eBasicTypeUnsignedInt128,
}

BASIC_TYPE_FMT = {
    lldb.eBasicTypeChar: lldb.eFormatDecimal,
    lldb.eBasicTypeSignedChar: lldb.eFormatDecimal,
    lldb.eBasicTypeUnsignedChar: lldb.eFormatUnsigned,
}

TUPLE_DELIM = "()"
STRUCT_DELIM = "{}"
SEQUECNE_DELIM = "[]"
ITEM_SEP = ","


def get_var(var_name: str) -> SBValue:
    var: SBValue = lldb.frame.var(var_name)
    assert var.IsValid(), f"Unable to find variable: {var_name}"

    return var


def check(expected: Variable):
    valobj: SBValue = get_var(expected.name)

    check_layout(valobj, expected)
    check_format(valobj, expected)

    if valobj.GetType().GetBasicType() != eBasicTypeInvalid:
        check_primitive(valobj, expected)
    else:
        check_aggregate(valobj, expected)

    check_summary(valobj, expected.summary, expected.pretty_print)


def check_layout(valobj: SBValue, expected: Variable):
    type = valobj.GetType()
    exp_type = expected.type

    assert type.GetByteSize() == exp_type.size
    assert type.GetByteAlign() == exp_type.align
    assert len(type.fields) == len(exp_type.fields)

    fields = {Field(f.name, f.GetType().GetName(), f.GetOffsetInBytes()) for f in type.fields}

    assert fields == set(exp_type.fields)


def check_primitive(valobj: SBValue, expected: Variable | Child):
    data: SBData = valobj.GetData()

    type: SBType = valobj.GetType().GetCanonicalType()
    kind = type.GetBasicType()
    assert kind != lldb.eBasicTypeInvalid, f"{valobj.name} is not a primtive"

    if (fmt := TYPE_UNPACK_FMT.get(kind)) is None:
        raise Exception(f"Unexpected eBasicType: {kind}")

    buf = data.ReadRawData(lldb.SBError(), 0, data.GetByteSize())

    if data.GetByteOrder() == lldb.eByteOrderBig or kind == lldb.eBasicTypeChar32:
        endian = ">"
    else:
        endian = "<"

    got = unpack(endian + fmt, buf)[0]
    exp_val = expected.value

    if kind == lldb.eBasicTypeChar32:
        got = got.decode("utf-32")

    assert got == exp_val, (
        f'var "{valobj.GetName()}": value does not match. Got: {got}, expected: {exp_val} ("{expected}")'
    )


def check_format(valobj: SBValue, expected: Variable):
    fmt = valobj.GetTypeFormat().GetFormat() if valobj.GetTypeFormat().IsValid() else None

    assert fmt == expected.format, (
        f"Invalid format for type {valobj.GetTypeName()}. Got: {valobj.GetTypeFormat()}"
    )


def check_aggregate(valobj: SBValue, expected: Variable):
    synth = valobj.GetTypeSynthetic().GetData() if valobj.GetTypeSynthetic().IsValid() else None
    expected_synth = expected.synthetic

    assert synth == expected_synth, (
        f"Unexpected SyntheticProvider. Got: {synth}, expected: {expected_synth}"
    )

    if synth is not None:
        check_synthetic(valobj, synth, expected)


def check_synthetic(valobj: SBValue, synth_provider: str, expected: Variable):
    (_mod_name, _, provider_name) = synth_provider.partition(".")
    provider = getattr(lldb_lookup, provider_name)
    # make sure __init__ doesn't throw an exception
    synth = provider(valobj.GetNonSyntheticValue(), {})

    if getattr(synth, "get_child_index", None) is None:
        raise Exception(
            f"Synthetic '{provider.__name__}' missing required method `get_child_index`"
        )
    if getattr(synth, "get_child_at_index", None) is None:
        raise Exception(
            f"Synthetic '{provider.__name__}' missing required method `get_child_at_index`"
        )

    check_children(valobj, synth, expected.children)


def check_children(valobj: SBValue, synth, expected: dict[str, Child]):
    child_count = valobj.GetNumChildren()
    expected_child_count = len(expected)
    assert child_count == expected_child_count, (
        f"Expected {expected_child_count} children, got {child_count}"
    )

    errors = []

    for k, v in expected.items():
        child = valobj.GetChildMemberWithName(k)
        if not child.IsValid():
            errors.append(f"Cannot find child: {valobj.GetName()}.{k}")
            continue

        if child.TypeIsPointerType():
            continue

        if isinstance(v, dict):
            check_children(child, synth, v)
        else:
            try:
                check_primitive(child, v)
            except Exception as e:
                errors.append(str(e))

    if len(errors) != 0:
        raise Exception("\n".join(errors))


def check_summary(valobj: SBValue, provider_name: Optional[str], expected: Optional[str]):
    sb_summary = valobj.GetTypeSummary()
    assert (
        provider_name is None and not sb_summary.IsValid()
    ) or valobj.GetTypeSummary().GetData() == provider_name, (
        f"Unexpected SummaryProvider. Got: '{valobj.GetTypeSummary().GetData()}', expected: '{provider_name}'"
    )

    assert valobj.GetSummary() == expected, (
        f"Summary does not match. Got: '{valobj.GetSummary()}', expected: '{expected}'"
    )
