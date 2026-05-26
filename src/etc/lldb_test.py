# IMPORTANT: we cannot use from __future__ import annotations because we rely on the runtime class
# information from type hints. `from __future import annotations` converts type hints to strings,
# and it's a bit more trouble to pull the class information that way.
# from __future__ import annotations

from typing import get_origin
from dataclasses import field
import sys
import enum
import os
import json

from dataclasses import dataclass, asdict, is_dataclass, fields
from struct import unpack
from typing import TypeAlias, Optional, Any

import lldb_lookup
import lldb
from lldb import SBValue, SBType, SBData, eBasicTypeInvalid

char: TypeAlias = str
Primitive: TypeAlias = int | float | bool | char

# see: default json decoder docs https://docs.python.org/3/library/json.html#json.JSONDecoder
# The types we're dealing with can only be: int, str, float, list, dict, bool, and None
JsonType: TypeAlias = (
    int | str | float | list["JsonType"] | bool | None | dict[str, "JsonType"]
)
# JsonDict: TypeAlias = dict[str, JsonType]

FRAME = lldb.frame
BLESS = os.environ["LLDB_BATCHMODE_BLESS_TEST_DATA"] == "1"


class Target(enum.StrEnum):
    """Due to the differences between PDB and DWARF debug info, we cannot guarantee their output
    will be identical. Since LLDB can handle both, we need to conditionally select the correct
    test data to use.

    Additionally, since there are differences in the internals of some structs based on OS (e.g.
    `PathBuf`/`OsString`), we need to be aware of whether we're on Windows or not.

    A global var `TARGET` is set to the current variant upon `lldb_test.py`'s instantiation using an
    env var passed from `compiletest` and is not expected to change afterwards."""

    NonWindowsGnu = "non_windows_gnu"
    WindowsGnu = "windows_gnu"
    WindowsMsvc = "windows_msvc"


def get_target() -> Target:
    # set by compiletest when launching LLDB
    t: str = os.environ["LLDB_BATCHMODE_TARGET_TRIPLE"]
    if t.endswith("windows-gnu") or t.endswith("windows-gnullvm"):
        return Target.WindowsGnu

    if t.endswith("windows-msvc"):
        return Target.WindowsGnu

    if t.endswith("-gnu"):
        return Target.NonWindowsGnu

    raise Exception(f"Unable to classify target '{t}'")


TARGET: Target = get_target()
"""The target this test was compiled for. This variable should never be changed after
initialization."""


def from_dict(ty: type[Any], data: JsonType):
    """Translates a dictionary into an instance of the given dataclass type (with possibly nested
    dataclasses).

    Relies on accurate type hints for the dataclass's fields, and the standard `dataclass.__init__`
    definition."""
    if get_origin(ty) is Optional:
        ty = ty.__args__[0]

    # recurse into lists
    if isinstance(data, list):
        # pulls the generic type from the list (e.g. `list[int]` -> `int`)
        (inner,) = ty.__args__
        print(inner)
        return [from_dict(inner, i) for i in data]

    if get_origin(ty) is dict and ty.__args__[0] is str:
        assert isinstance(data, dict)

        if ty.__args__[1] is Variable:
            return {k: from_dict(Variable, data[k]) for k in data.keys()}
        if ty.__args__[1] is Child:
            return {k: from_dict(Child, data[k]) for k in data.keys()}

    # map dict -> dataclass, recursing for each field
    if is_dataclass(ty):
        assert isinstance(data, dict)

        field_types = {f.name: f.type for f in fields(ty)}

        # if you've never seen this before, `**` is the splat operator. It expands a mapping type
        # (in this case a dict) to keyword arguments. The ordering of the mapping does not matter,
        # only that the mapping's keys match the functions keyword args, and `len(mapping)` == the
        # number of keyword args.
        try:
            field_map = {}

            for f in data:
                f_type = field_types[f]

                # type annotations can be strings, so we need to resolve them to their actual type
                if isinstance(f_type, str):
                    # First we try stripping any generics that may exist and look up the type in the
                    # builtins. This catchs int, float, bool, None, str, list, and dict. If none of
                    # those were found, we look up the type within this module.
                    f_type = globals().get(f_type) or getattr(
                        __builtins__, f_type.split("[", 1)[0]
                    )

                field_map[f] = from_dict(f_type, data[f])

            return ty(**field_map)
        except KeyError as e:
            print(
                f"Unable to convert dict to {ty}: Invalid field name {e}. If the test schema was \
changed intentionally, use the `--bless` option to update test data to the new schema."
            )

    # for any other type, we don't need to do any processing
    return data


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
class Field:
    name: str
    type: str
    offset: int

    @staticmethod
    def from_lldb(field: lldb.SBTypeMember) -> "Field":
        return Field(
            field.GetName(), field.GetType().GetName(), field.GetOffsetInBytes()
        )


@dataclass(slots=True)
class Type:
    name: str
    pretty_name: str
    size: int
    align: int
    basic_type: int
    type_class: int
    fields: list[Field]
    # FIXME Rust doesn't output template *values* (the `10` in `ArrayVec<u8, 10>`), only template
    # args (the `u8` in `ArrayVec<u8, 10>`). If that ever changes, we need to update the logic for
    # this.
    # generic_params: list[Type | Primitive]
    # FIXME we can only look up static fields by name as of lldb 22, so we need a way to discover
    # them. ATM only sum-type enums on MSVC use static fields , so it's not super important
    # static_fields: list[StaticField]

    @staticmethod
    def from_lldb(type: SBType) -> "Type":
        return Type(
            type.GetName(),
            type.GetDisplayTypeName(),
            type.GetByteSize(),
            type.GetByteAlign(),
            type.GetBasicType(),
            type.GetTypeClass(),
            [
                Field.from_lldb(type.GetFieldAtIndex(i))
                for i in range(type.GetNumberOfFields())
            ],
        )
        # FIXME (todo) template args
        # self.generic_params = [lldb_lookup.get_template_args()]


@dataclass(slots=True)
class Child:
    """Similar to `Variable`, but carries less information since we primarily test top-level
    values"""

    type: str
    value: Primitive | dict[str, "Child"]

    @staticmethod
    def from_lldb(child: SBValue) -> "Child":
        type: SBType = child.GetType()

        if type.GetBasicType() == lldb.eBasicTypeInvalid:
            value = {}
            for i in range(child.GetNumChildren()):
                c = child.GetChildAtIndex(i)
                value[c.GetName()] = Child.from_lldb(c)
        else:
            value = decode_primitive(child)

        return Child(child.GetType().GetName(), value)


@dataclass(slots=True)
class Variable:
    """
    ~1:1 mapping from the json to a python class for convenience and consistency.
    """

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
    def from_lldb(var: SBValue) -> "Variable":
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
            type,
            pretty_print,
            value,
            synthetic,
            summary,
            format,
            children,
        )


@dataclass(slots=True)
class BlessMetadata:
    """
    Contains additional context about the tools at the time the test data was generated
    """

    python_version: str = ""
    lldb_version: str = ""
    # FIXME (todo)
    # lldb_feature_flags: str


@dataclass(slots=True)
class TargetData:
    """
    Per-target test data.
    """

    bless_metadata: BlessMetadata = field(default_factory=BlessMetadata)
    breakpoints: list[dict[str, Variable]] = field(default_factory=list)
    """Each element corresponds to one stopping point in the test. The element itself is a
    dictionary mapping variable names to their respective test data."""


@dataclass(slots=True, init=False)
class TestData:
    """
    Top-level container for all test data.

    Due to the differences between PDB and DWARF debug info, we cannot guarantee their output
    will be identical. Since LLDB can handle both, we need to conditionally select the correct
    test data to use.

    Additionally, since there are differences in the internals of some structs based on OS (e.g.
    `PathBuf`/`OsString`), we need to be aware of whether we're on Windows or not.

    A global var `TARGET` is set to the current variant upon `lldb_test.py`'s instantiation using an
    env var passed from `compiletest` and is not expected to change afterwards.
    """

    non_windows_gnu: TargetData
    windows_gnu: TargetData
    windows_msvc: TargetData

    def __init__(self):
        path = os.environ["LLDB_BATCHMODE_INPUT_DATA_PATH"]
        if not os.path.isfile(path):
            self.non_windows_gnu = TargetData()
            self.windows_gnu = TargetData()
            self.windows_msvc = TargetData()

            return

        with open(path, "r") as f:
            data = json.load(f)

        if BLESS and TARGET == Target.WindowsGnu:
            self.windows_gnu = TargetData()
        else:
            self.windows_gnu = from_dict(TargetData, data["windows_gnu"])

        if BLESS and TARGET == Target.WindowsMsvc:
            self.windows_msvc = TargetData()
        else:
            self.windows_msvc = from_dict(TargetData, data["windows_msvc"])

        if BLESS and TARGET == Target.NonWindowsGnu:
            self.non_windows_gnu = TargetData()
        else:
            self.non_windows_gnu = from_dict(TargetData, data["non_windows_gnu"])

    def get_target_data(self) -> TargetData:
        """Retrieves data from the target specified by `compiletest`"""

        if TARGET == Target.WindowsGnu:
            return self.windows_gnu

        if TARGET == Target.WindowsMsvc:
            return self.windows_msvc

        return self.non_windows_gnu

    def bless_variable(self, var_name: str, breakpoint_idx: int):
        """Updates the mapping with data generated from the given variable. Only affects the mapping
        of the current target and breakpoint. This function **does not** write to the input file.
        Update all necessary vars, then write the entire input data at once using
        `RawInputData.save_blessing`"""
        valobj = FRAME.FindVariable(var_name)
        if not valobj.IsValid():
            # FIXME (todo) error handling
            raise Exception(f"<bless error: Cannot find variable {var_name}>")

        target_data = self.get_target_data()

        if len(target_data.breakpoints) <= breakpoint_idx:
            target_data.breakpoints.append({})

        target_data.breakpoints[breakpoint_idx][var_name] = Variable.from_lldb(valobj)

        print(self)

    def save_blessing(self):
        """Writes the entirety of `self` to the input file. Used to finalize changes made by
        one or more `RawInputData.bless_variable` calls."""
        print("saving")

        self.get_target_data().bless_metadata = BlessMetadata(
            sys.version, lldb.debugger.GetVersionString()
        )
        path = os.environ["LLDB_BATCHMODE_INPUT_DATA_PATH"]
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=" ")


INPUT_DATA = TestData()


def get_summary_or_value(valobj: SBValue) -> str | None:
    summary = valobj.GetSummary()
    if summary is None:
        return valobj.GetValue()

    return summary


def run(var_name: str, breakpoint: int, frame: lldb.SBFrame):
    global FRAME
    FRAME = frame

    if BLESS:
        print("blessing")
        INPUT_DATA.bless_variable(var_name, breakpoint)

    check(var_name, INPUT_DATA.get_target_data().breakpoints[breakpoint][var_name])

    print(f"{var_name}: Ok")


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
    var: SBValue = FRAME.var(var_name)
    assert var.IsValid(), f"Unable to find variable: {var_name}"

    return var


def check(var_name: str, expected: Variable):
    valobj: SBValue = get_var(var_name)

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

    fields = {
        Field(f.name, f.GetType().GetName(), f.GetOffsetInBytes()) for f in type.fields
    }

    assert fields == set(exp_type.fields)


def check_primitive(valobj: SBValue, expected: Variable | Child):
    data: SBData = valobj.GetData()

    type: SBType = valobj.GetType().GetCanonicalType()
    kind = type.GetBasicType()
    assert kind != lldb.eBasicTypeInvalid, f"{valobj.name} is not a primtive"

    parsed = Variable.from_lldb(valobj)
    assert parsed == expected

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

    assert (
        got == exp_val
    ), f'var "{valobj.GetName()}": value does not match. Got: {got}, expected: {exp_val} \
("{expected}")'


def check_format(valobj: SBValue, expected: Variable):
    fmt = (
        valobj.GetTypeFormat().GetFormat() if valobj.GetTypeFormat().IsValid() else None
    )

    assert (
        fmt == expected.format
    ), f"Invalid format for type {valobj.GetTypeName()}. Got: {valobj.GetTypeFormat()}"


def check_aggregate(valobj: SBValue, expected: Variable):
    synth = (
        valobj.GetTypeSynthetic().GetData()
        if valobj.GetTypeSynthetic().IsValid()
        else None
    )
    expected_synth = expected.synthetic

    assert (
        synth == expected_synth
    ), f"Unexpected SyntheticProvider. Got: {synth}, expected: {expected_synth}"

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


def check_children(valobj: SBValue, synth: str, expected: dict[str, Child]):
    child_count = valobj.GetNumChildren()
    expected_child_count = len(expected)
    assert (
        child_count == expected_child_count
    ), f"Expected {expected_child_count} children, got {child_count}"

    errors: list[str] = []

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


def check_summary(
    valobj: SBValue, provider_name: Optional[str], expected: Optional[str]
):
    sb_summary = valobj.GetTypeSummary()
    assert (
        (provider_name is None and not sb_summary.IsValid())
        or valobj.GetTypeSummary().GetData() == provider_name
    ), f"Unexpected SummaryProvider. Got: '{valobj.GetTypeSummary().GetData()}', expected: \
'{provider_name}'"

    assert (
        provider_name is None or valobj.GetSummary() == expected
    ), f"Summary does not match. Got: '{valobj.GetSummary()}', expected: '{expected}'"
