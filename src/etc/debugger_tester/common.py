"""Contains the class definitions outlining the schema of the test data. For LLDB conversion
from/into these types, see `./from_lldb.py`"""

import json
import os
import sys
import traceback
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from enum import Enum
from functools import reduce
from pprint import pformat

# `_eval_type` is what python uses internally to resolve type annotations at runtime. It more or
# less takes a string type annotation and looks it up in `globals()` and `locals()`. It's "private",
# but nothing actually stops us from importing it and using it for our own nefarious purposes.
from typing import Any, Final, Optional, Union, _eval_type, get_origin


class DbgType(ABC):
    @abstractmethod
    def name(self) -> str: ...
    @abstractmethod
    def type_from_dbg(self) -> "Type": ...
    @abstractmethod
    def field_types(self) -> list["DbgType"]: ...
    @abstractmethod
    def generics(self) -> list["DbgType"]: ...
    @abstractmethod
    def basic_type(self, raw: int | None) -> str: ...
    @abstractmethod
    def type_class(self, raw: int | None) -> str: ...


class DbgValue(ABC):
    @abstractmethod
    def type(self) -> DbgType: ...
    @abstractmethod
    def children(self) -> list["DbgValue"]: ...
    @abstractmethod
    def name(self) -> str: ...
    @abstractmethod
    def error_hint(self, for_mismatch: str) -> str: ...
    @abstractmethod
    def invoke_summary_provider(self, import_path: str): ...
    @abstractmethod
    def invoke_synthetic_provider(self, import_path: str): ...


char = str
Primitive = Union[int, float, bool, char]
ByteSize = int

# see: default json decoder docs https://docs.python.org/3/library/json.html#json.JSONDecoder
# The types we're dealing with can only be: int, str, float, list, dict, bool, and None
JsonType = Union[int, str, float, list["JsonType"], bool, None, dict[str, "JsonType"]]


class Result(Enum):
    Ok = True
    Mismatch = False

    def __and__(self, other: "Result") -> "Result":
        return Result(self.value & other.value)

    def __bool__(self) -> bool:
        return self.value


ANSI_RED = "\033[91m"
ANSI_END = "\033[0m"


def print_error(error_source: str, message: str):
    print(f"{ANSI_RED}  [repr error: {error_source}]{ANSI_END} {message}")


def format_mismatch(label: str, got: Optional[Any], expected: Optional[Any]) -> str:
    if got is None and expected is not None:
        return f"{label} not found, expected: {expected}"
    elif expected is not None and got is None:
        return f"{label} '{got}' found when none was expected."
    else:
        return f"{label} does not match.\n    Expected: {expected}\n    Got: {got}"


def print_mismatch(
    error_source: str, label: str, got: Optional[Any], expected: Optional[Any]
):
    print_error(error_source, format_mismatch(label, got, expected))


class Target(Enum):
    """Due to the differences between PDB and DWARF debug info, we cannot guarantee their output
    will be identical. Since LLDB can handle both, we need to conditionally select the correct
    test data to use.

    Additionally, since there are differences in the internals of some structs based on OS (e.g.
    `PathBuf`/`OsString`), we need to be aware of whether we're on Windows or not.

    A global var `TARGET` is set to the current variant upon `lldb_test.py`'s instantiation using an
    env var passed from `compiletest` and is not expected to change afterwards."""

    NonWindows = "non_windows"
    WindowsGnu = "windows_gnu"
    WindowsMsvc = "windows_msvc"


def get_target() -> Target:
    # set by compiletest when launching LLDB
    t: str = os.environ["DEBUGGER_TESTER_TARGET_TRIPLE"]

    if t.endswith("windows-msvc"):
        return Target.WindowsMsvc
    if t.endswith(("windows-gnu", "windows-gnullvm")):
        return Target.WindowsGnu

    return Target.NonWindows


BLESS: Final[bool] = os.environ["DEBUGGER_TESTER_BLESS_TEST_DATA"] == "1"
"""Global constant set by `compiletest` that determines whether or not we are blessing the test
data."""


TARGET: Final[Target] = get_target()
"""Global constant set by `compiletest`. Determines which target the tests were run for, thus which
set of test input we check."""


def from_dict(ty: type[Any], data: JsonType):
    """Translates a dictionary into an instance of the given dataclass type (with possibly nested
    dataclasses).

    Relies on accurate type hints for the dataclass's fields, and the default `dataclass.__init__`
    definition."""

    ty = _eval_type(ty, globals(), locals())

    origin = get_origin(ty) or ty

    # Optional isn't a constructor, so we have to "unwrap" it.
    if origin == Union and len(ty.__args__) == 2 and ty.__args__[1] == type(None):
        ty = ty.__args__[0]

    # Special handling for array-like children space optimization
    if (
        ty == Union[list[Child], ArrayLikeChildren, type(None)]
        or ty == Union[list["Child"], ArrayLikeChildren, type(None)]
    ):
        if isinstance(data, dict):
            al_type = data["type"]
            vals: Union[list[dict], list[Primitive]] = data["vals"]
            # if we have a list of dict, we know we have array children
            # otherwise, we have a list of primitives that can be used
            # as-is
            if len(vals) != 0 and isinstance(vals[0], dict):
                vals = [from_dict(ArrayChild, v) for v in vals]

            return ArrayLikeChildren(al_type, vals)
        elif isinstance(data, list):
            return [from_dict(Child, i) for i in data]

    # recurse into lists
    if isinstance(data, list):
        # pulls the generic type from the list (e.g. `list[int]` -> `int`)
        inner = ty.__args__[0]

        return [from_dict(inner, i) for i in data]

    if origin == dict and ty.__args__[0] == str:
        assert isinstance(data, dict)
        val_ty = ty.__args__[1]

        if val_ty in [Variable, Child, Type, Field, ArrayChild]:
            return {k: from_dict(val_ty, data[k]) for k in data}

    # map dict -> dataclass, recursing for each field
    if is_dataclass(ty):
        assert isinstance(data, dict)

        field_types = {f.name: f.type for f in fields(ty)}

        try:
            field_map = {}

            for f in data:
                f_type = field_types[f]

                field_map[f] = from_dict(f_type, data[f])

            # if you've never seen this before, `**` is the splat operator. It expands a mapping
            # type (in this case a dict) to keyword arguments. The ordering of the mapping does not
            # matter, only that the mapping's keys match the functions keyword args, and
            # `len(mapping)` == the number of keyword args.
            return ty(**field_map)
        except KeyError as e:
            print(
                f"Unable to convert dict to {ty}: Invalid field name {e}. If the test schema was \
changed intentionally, use the `--bless` option to update test data to the new schema."
            )

    # for any other type, we don't need to do any processing
    return data


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    """The fully qualified name of the field's type. Full type information should be looked up
    via `TargetData.types`"""

    offset: ByteSize


@dataclass
class Type:
    size: ByteSize
    # When GDB support is added to the test framework, basic_type and type_class will probably be
    # converted to a wrapper IntEnum that converts GDB's equivalent information to
    type_class: int
    """The `lldb.eTypeClass` value associated with thjs type. Tested due to our use of it in type
    recognizer functions."""

    basic_type: Optional[int] = None
    """The `lldb.eBasicType` value associated with this type. Tested due to our use of it in type
    recognizer functions."""

    fields: Optional[list[Field]] = field(default_factory=list)
    """Stored as a list due to our reliance on `SBType.GetFieldAtIndex()`

    Note: LLDB **does not** reorder the fields of a type based on their offset. For example,
    `GetFieldAtIndex(0).GetByteOffset()` may return `8`. Instead, the order of the fields is a
    direct reflection of their ordering in the debug info (which, as far as I know, is the same as
    their declaration order in the source code).
    """

    generic_params: Optional[list[str]] = field(default_factory=list)
    """Stored as a list due to our reliance on `SBType.GetTemplateArgumentType()` and the sequential
    behavior of `lldb_providers.get_template_args`"""
    # FIXME the only way we can look up static fields is by name (as of lldb 22), so we need a way
    # to discover them. ATM only sum-type enums on MSVC use static fields, and those are fixed
    # values, so it's not super urgent.
    # static_fields: list[StaticField]

    def print_field_errors(self, expected: "Type", error_source: str):
        """Extra processing for better error messages. The following common cases are covered:
        * New/Missing fields
        * Source code rearranged fields
        * Rustc rearranged fields
        * Renamed fields

        If none of the common cases are encountered, we just generically print any mismatched
        fields.
        """

        # FIXME these checks aren't exactly the most efficient they could be. Luckily, the happy
        # path skips this function entirely, so passing tests are still fast. These checks could
        # probably all be done in 2ish total iters over each list, but optimization isn't a huge
        # concern at the moment.

        got_set = set(self.fields)
        expected_set = set(expected.fields)

        if len(self.fields) != len(expected.fields):
            new_fields = got_set.difference(expected_set)

            missing_fields = expected_set.difference(got_set)

            if len(missing_fields) != 0:
                print_error(
                    error_source,
                    f"The following field(s) appear to have been removed from the type:\n\
{missing_fields}",
                )

            if len(new_fields) != 0:
                print_error(
                    error_source,
                    f"The following field(s) appear to have been added to the type:\n\
{new_fields}",
                )

        # are all of the same fields present, regardless of order? If so, they were rearranged
        # in the source code, but the compiler kept the same ordering.
        elif got_set == expected_set:
            print_error(
                error_source,
                f"Field(s) appear to have been rearranged:\n    Expected:\n\
{pformat(self.fields, indent=6)}\n    Got:\n{pformat(expected.fields, indent=6)}",
            )
        else:
            # we know for sure that both sets of fields are the same length, but some parts of one
            # or more fields don't match
            types_match = True
            offsets_match = True
            names_match = True
            mismatches: list[tuple[Field, Field]] = []

            for g, e in zip(self.fields, expected.fields):
                if g.type != e.type:
                    types_match = False
                    mismatches.append((g, e))
                if g.offset != e.offset:
                    offsets_match = False
                    mismatches.append((g, e))
                if g.name != e.name:
                    names_match = False
                    mismatches.append((g, e))

            # If the types and offsets are the same but the names aren't, we know fields have
            # been renamed.
            if types_match and offsets_match:
                renames = "\n    ".join(
                    f"{m[1].name} -> {m[0].name}" for m in mismatches
                )
                print_error(
                    error_source,
                    f"The following field(s) appear to have been renamed (expected -> got):\n\
    {renames}",
                )

            # If the types and names are the same, but the offsets are different, we know that rustc
            # has decided to order the fields differently, despite the source code not changing
            elif types_match and names_match:
                reordered = "\n    ".join(
                    (
                        f"{m[1].name} offset: +{m[1].offset} -> {m[0].name} offset: +{m[0].offset}"
                    )
                    for m in mismatches
                )

                print_error(
                    error_source,
                    f"The following field(s) appear to have been reordered by rustc (expected -> \
got):\n    {reordered}",
                )

            else:
                mm_string = "\n    ".join(f"{m[1]} -> {m[0]}" for m in mismatches)

                print_error(
                    error_source,
                    f"The following field(s) do not match (expected -> got):\n\
    {mm_string}",
                )


@dataclass
class ArrayChild:
    """A child in `ArrayLikeChildren`"""

    value: Optional[Primitive] = None
    children: Optional[Union[list["Child"], "ArrayLikeChildren"]] = None


@dataclass
class ArrayLikeChildren:
    """Visualizers commonly output children in the form `[0]=<value>, [1]=<value>, ...` where the
    types of all elements are the same. This container allows for space-optimizing the output data
    by only storing the type once, and not storing the names of the children. During comparison,
    `ArrayLikeChildren` are converted to the equivalent `list[Child]` and compared as normal.

    To detect if a set of children are array-like, use `is_arraylike(children)`. To convert an
    array-like list into `ArrayLikeChildren`, use `make_arraylike(children)`
    """

    type: str
    vals: Union[list[ArrayChild], list[Primitive]] = field(default_factory=list)

    def __len__(self):
        return len(self.vals)

    def __getitem__(self, idx):
        return self.vals[idx]


@dataclass
class Child:
    """Similar to `Variable`, but carries less information since we primarily test top-level
    values (and assume values of these child types have been tested thoroughly elsewhere).

    Note that if the type has a synthetic provider (lldb) or pretty printer (gdb), the child names
    and types can be set to anything at all, so we do need to test these separately from the
    parent's type's fields."""

    name: str
    """The name used to access the child. If the parent object has a synthetic, the child name can
    be overridden."""

    type: str
    """The fully qualified name of the child's type. Full type information should be looked up
    via `TargetData.types`"""

    value: Optional[Primitive] = None
    children: Optional[Union[list["Child"], ArrayLikeChildren]] = field(
        default_factory=list
    )
    """Children are stored as a list because of our use of `GetChildAtIndex()`. Providers can also
    dictate the order that children populate, so it's important to ensure that stays consistent too.
    """


def is_arraylike(children: list[Child]) -> bool:
    """Returns true if every child is the same type, and if all of the child names are, in order,
    `[0]`, `[1]`, `[2]`, ...
    """
    if len(children) <= 0:
        return False

    first_type = children[0].type

    return all(
        x.name.startswith("[")
        and x.name.endswith("]")
        and int(x.name[1:-1]) == i
        and x.type == first_type
        for i, x in enumerate(children)
    )


def make_arraylike(children: list[Child]) -> ArrayLikeChildren:
    return ArrayLikeChildren(
        children[0].type,
        [
            ArrayChild(c.value, c.children) if len(c.children) != 0 else c.value
            for c in children
        ],
    )


@dataclass
class Variable:
    type: str
    """The fully qualified name of the variable's type. Full type information should be looked up
    via `TargetData.types`"""

    pretty_type_name: Optional[str] = None
    """Type names can be overridden by `SyntehticProvider.get_type_name()` in LLDB and by
    `type_printer` in GDB"""

    pretty_print: Optional[str] = None
    """The string-result of pretty printing the value (`SBValue.GetSummary` for LLDB,
    `pretty_printer.to_string` for GDB). `None` for aggregates with no summary provider."""

    value: Optional[Primitive] = None
    """`None` if the object does not have a primitive representation."""

    synthetic: Optional[str] = None
    """The class/function name of the synthetic provider (lldb) or pretty printer (gdb).
    `None` if the object does not have a synthetic provider"""

    summary: Optional[str] = None
    """The function name of the summary provider. `None` if the object does not have a summary
    provider, or if the test data is for GDB"""

    format: Optional[int] = None
    """The `lldb.eFormat` enum variant associated with this type (if applicable)."""

    # Stored as a list instead of a dict because child order matters
    children: Optional[Union[list[Child], ArrayLikeChildren]] = field(
        default_factory=list
    )
    """A list of children provided by the object. If the object has a synthetic provider, the
    children are the result of the provider's `get_child_at_index` function"""

    def has_visualizer(self) -> bool:
        return (
            self.synthetic is not None
            or self.summary is not None
            or self.format is not None
        )

    def matches_expected(self, expected: "Variable", dbg_val: DbgValue) -> Result:
        # Happy path requires very little intercession from us. We keep these values on the stack
        # so we don't have to recalculate them if we need to do error handling
        summary_ok = self.summary == expected.summary
        synthetic_ok = self.synthetic == expected.synthetic
        pretty_type_name_ok = self.pretty_type_name == expected.pretty_type_name
        pretty_print_ok = self.pretty_print == expected.pretty_print
        format_ok = self.format == expected.format

        all_providers_ok = (
            summary_ok
            & synthetic_ok
            & format_ok
            & pretty_type_name_ok
            & pretty_print_ok
        )

        type_ok = self.type == expected.type

        if self.has_visualizer() or expected.has_visualizer():
            type_match_ok = self.type_matches(dbg_val.type(), all_providers_ok)
        else:
            type_match_ok = Result.Ok

        # (small hack) If the type information doesn't match, BUT it doesn't affect the synthetics,
        # we don't actually care that much. This prevents CI from failing when 2 different targets
        # use 2 different layouts, e.g.:
        # x86_64-linux-gnu: `Vec { buf, len, marker}`
        # aarch64-linux-gnu: `Vec {len, buf, marker}`
        # if type_match_ok == Result.Mismatch and all_providers_ok:
        #     type_match_ok = Result.Ok

        value_ok = self.value == expected.value

        work_list = []
        work_list.extend(dbg_val.children())
        child_types_ok = Result.Ok

        while len(work_list) != 0:
            obj = work_list.pop()

            work_list.extend(obj.children())

            # similar to type matching, we don't actually care if there's a mismatch if the
            # providers aren't affected. We still want to report it though just in case.
            if self.has_visualizer() or expected.has_visualizer():
                child_types_ok &= self.type_matches(dbg_val.type(), all_providers_ok)
                if child_types_ok == Result.Mismatch and all_providers_ok:
                    child_types_ok = Result.Ok
            else:
                child_types_ok = Result.Ok

        children_ok = self.children_match(
            self.children, expected.children, dbg_val.name(), dbg_val
        )

        if (
            type_ok
            and type_match_ok
            and pretty_type_name_ok
            and pretty_print_ok
            and value_ok
            and synthetic_ok
            and summary_ok
            and format_ok
            and children_ok
            and child_types_ok
        ):
            return Result.Ok

        # otherwise, we want to output exactly what doesn't match
        # and any additional helpful information

        error_source = f"var '{dbg_val.name()}'"

        # We check the type first. If this has changed, it's relatively likely nothing else will
        # work properly
        if not type_ok:
            print_mismatch(
                error_source,
                f"type ({dbg_val.error_hint('type')})",
                self.type,
                expected.type,
            )

        # We check the summary next since it's the most user-visible output. We don't need to check
        # `pretty_print` if the summary provider doesn't match.
        if not summary_ok:
            print_mismatch(
                error_source,
                f"summary ({dbg_val.error_hint('summary')})",
                self.summary,
                expected.summary,
            )
        elif not pretty_print_ok:
            print_mismatch(
                error_source,
                f"pretty_print ({dbg_val.error_hint('pretty_print')})",
                self.pretty_print,
                expected.pretty_print,
            )

            # try the summary provider directly to see if it's throwing an exception
            if self.summary is not None:
                try:
                    dbg_val.invoke_summary_provider(self.summary)
                except Exception as e:  # noqa: BLE001
                    print_error(
                        error_source + " Summary",
                        "Error while running Summary Provider:",
                    )
                    traceback.print_exception(
                        type(e), e, e.__traceback__, file=sys.stdout
                    )

        # Next we check the value and formatter. These mostly affect primitives.
        if not value_ok:
            print_mismatch(
                error_source,
                f"value ({dbg_val.error_hint('value')})",
                self.value,
                expected.value,
            )
        if not format_ok:
            print_mismatch(
                error_source,
                f"format ({dbg_val.error_hint('format')})",
                self.format,
                expected.format,
            )

        # Synthetic is checked next since children, pretty type name, and pretty print rely on it.
        # If the synthetic doesn't match, we can assume those won't match either.
        if not synthetic_ok:
            print_mismatch(
                error_source,
                f"synthetic ({dbg_val.error_hint('synthetic')})",
                self.synthetic,
                expected.synthetic,
            )
        else:
            if not pretty_type_name_ok:
                print_mismatch(
                    error_source,
                    f"pretty_type_name ({dbg_val.error_hint('pretty_type_name')})",
                    self.pretty_type_name,
                    expected.pretty_type_name,
                )

            if not children_ok and self.synthetic is not None:
                # If the children don't match, we can check for more catastrophic failures using the
                # synthetic provider. All the per-children errors will have been printed in the
                # `children_match` check above.
                try:
                    dbg_val.invoke_synthetic_provider(self.synthetic)
                except Exception as e:  # noqa: BLE001
                    print_error(
                        error_source + " Synthetic",
                        "Error while running Synthetic Provider:",
                    )
                    traceback.print_exception(
                        type(e), e, e.__traceback__, file=sys.stdout
                    )

        return Result.Mismatch

    def type_matches(self, dbg_ty: DbgType, provider_ok: bool = False) -> Result:
        name: str = dbg_ty.name()

        if (r := TYPES_TESTED.get(name)) is not None:
            # The proper result was returned the first time the type was tested, so we can just
            # pretend everything we've already seen has succeeded.
            if not r:
                print_error(
                    f"type '{name}'", f"mismatch (see prior output for type '{name}')"
                )
            return r

        error_source = f"type '{name}'"

        ty = dbg_ty.type_from_dbg()
        expected = INPUT_DATA.types.get(name)

        result = Result.Ok

        if expected is None:
            print_error(f"type '{name}'", "type not found in input data")
            return Result.Mismatch

        if ty.basic_type != expected.basic_type:
            result = Result.Mismatch
            print_mismatch(
                error_source,
                "basic_type",
                f"{ty.basic_type} ({dbg_ty.basic_type(ty.basic_type)})",
                f"{expected.basic_type} ({dbg_ty.basic_type(expected.basic_type)})",
            )

        if ty.type_class != expected.type_class:
            result = Result.Mismatch
            print_mismatch(
                error_source,
                "type_class",
                f"{ty.type_class} ({dbg_ty.type_class(ty.type_class)})",
                f"{expected.type_class} ({dbg_ty.type_class(expected.type_class)})",
            )

        # FIXME handle 32 bit targets
        if ty.size != expected.size:
            result = Result.Mismatch
            print_mismatch(error_source, "size", ty.size, expected.size)

        if ty.fields != expected.fields:
            result = Result.Mismatch
            ty.print_field_errors(expected, error_source)

        if ty.generic_params != expected.generic_params:
            result = Result.Mismatch
            print_mismatch(
                error_source,
                "generic_params",
                ty.generic_params,
                expected.generic_params,
            )

        if result == Result.Mismatch and provider_ok:
            print_error(
                error_source,
                "It appears these changes do not affect the type's providers. Consider rerunning \
with the `--bless` option",
            )

        TYPES_TESTED[name] = result

        result = reduce(
            lambda x, y: x and y,
            (self.type_matches(t, provider_ok) for t in dbg_ty.field_types()),
            result,
        )

        result = reduce(
            lambda x, y: x and y,
            (self.type_matches(t, provider_ok) for t in dbg_ty.generics()),
            result,
        )

        return result

    @staticmethod
    def children_match(
        children: list[Child],
        expected: list[Child],
        path: str,
        dbg_val: DbgValue,
    ) -> Result:
        """
        Recursively checks children against an expected value and prints errors for mismatches.
        """

        result = Result.Ok if len(children) == len(expected) else Result.Mismatch

        mismatches = []
        missing = []
        invalid_count = 0

        for i in range(len(expected)):
            exp = expected[i]

            if i >= len(children):
                missing.append(exp.name)
                continue

            got = children[i]

            if isinstance(children, ArrayLikeChildren):
                if isinstance(got, ArrayChild):
                    got = Child(f"[{i}]", children.type, got.value, got.children)
                else:
                    got = Child(f"[{i}]", children.type, got, [])

            if isinstance(expected, ArrayLikeChildren):
                if isinstance(exp, ArrayChild):
                    exp = Child(f"[{i}]", expected.type, exp.value, exp.children)
                else:
                    exp = Child(f"[{i}]", expected.type, exp, [])

            if got.name is None:
                result = Result.Mismatch
                invalid_count += 1
                mismatches.append(
                    f"{exp.name}: {exp.type} = {exp.value} -> <Invalid SBValue>"
                )
            elif got.name != exp.name or got.type != exp.type or got.value != exp.value:
                result = Result.Mismatch
                mismatches.append(
                    f"{exp.name}: {exp.type} = {exp.value} -> {got.name}: {got.type} = {got.value}"
                )
            # no point recursing into children if we've already mismatched
            elif exp.children is not None and len(exp.children) > 0:
                result &= Variable.children_match(
                    got.children,
                    exp.children,
                    f"{path}.{exp.name}",
                    dbg_val.children()[i],
                )

        if result == Result.Ok:
            return result

        # If every single child is invalid, we can condense the output a lot by pointing to the
        # synthetic instead of printing a bunch of identical mismatches
        if invalid_count == len(children):
            print_error(
                path,
                "All children of this object are invalid SBValue objects.\n    This is \
    almost always caused by invalid state or logic in the SyntheticProvider.",
            )
        elif len(mismatches) != 0:
            error_str = "\n    ".join(mismatches)
            print_error(
                path,
                f"The following children do not match (expected -> got):\n    {error_str}",
            )
        elif len(missing) != 0:
            error_str = ", ".join(missing)
            print_error(
                path,
                f"The following children were expected, but were not found:\n    {error_str}",
            )
        elif len(children) > len(expected):
            error_str = "\n    ".join(
                f"{got.name}: {got.type} = {got.value}"
                for got in children[len(expected) :]
            )
            print_error(
                path,
                f"The following children were found, but were not expected:\n    {error_str}",
            )

        return result


@dataclass
class BlessMetadata:
    """
    Contains additional context about the tools at the time the test data was generated.
    """

    python_version: str = ""
    debugger_version: str = ""
    feature_flags: str = ""


@dataclass
class TargetData:
    """
    Top-level container for all test data.

    Due to the differences between PDB and DWARF debug info, we cannot guarantee their output
    will be identical. Since LLDB can handle both, we need to conditionally select the correct
    test data to use.

    Additionally, since there are differences in the internals of some structs based on OS (e.g.
    `PathBuf`/`OsString`), we need to be aware of whether we're on Windows or not.

    A global var `TARGET` is set to the current variant upon `debugger_tester`'s instantiation using
    an env var passed from `compiletest` and is not expected to change afterwards.
    """

    bless_metadata: BlessMetadata = field(default_factory=BlessMetadata)
    """Miscellaneous data included to make diagnosing issues easier. This data is not intended to be
    tested against."""

    # If we ever decide that it makes sense to check the same variable twice at the same breakpoint
    # this will need to be converted to a list
    breakpoints: list[dict[str, Variable]] = field(default_factory=list)
    """Each element corresponds to one stopping point in the test. The element itself is a
    dictionary mapping variable names to their respective test data."""

    types: dict[str, Type] = field(default_factory=dict)
    """
    A map of type names to types. Contains all types present in the test's variables, including the
    types of fields and child objects.
    """

    @staticmethod
    def initialize() -> "TargetData":
        result = TargetData()
        path = os.environ["DEBUGGER_TESTER_INPUT_DATA_PATH"]
        if not os.path.isfile(path):
            if BLESS:
                return result
            else:
                raise Exception(
                    f"Invalid input data path: '{path}'\nIf test data has not been \
generated for this test yet, consider using the `--bless` option."
                )

        if BLESS:
            return result

        with open(path, "r") as f:
            try:
                result = from_dict(TargetData, json.load(f))
            except json.decoder.JSONDecodeError:
                print("Warning: Malformed input data, reverting to default")

        return result

    def save_blessing(self, metadata: BlessMetadata):
        """Writes the entirety of `self` to the env var `DEBUGGER_TESTER_INPUT_DATA_PATH`, which is
        set by `compiletest` before running `debugger_tester`. Used to finalize changes made by one
        or more `from_lldb.bless_variable` calls.

        This function should be called exactly once, right before
        `debugger_tester.lldb.batchmode.main` exits if the following conditions are met:

        1. No other exceptions or error states occurred
        2. `BLESS == True`
        3. At least one `repr` pseudo-command was processed

        This prevents us from saving incomplete data or invalid data. It also prevents us from
        creating input data files for tests that do not need it.
        """

        self.bless_metadata = metadata
        path = os.environ["DEBUGGER_TESTER_INPUT_DATA_PATH"]

        # We first sort the types dictionary. This keeps the git diff more consistent, and doesn't
        # affect runtime behavior at all.
        import operator

        self.types = dict(sorted(self.types.items(), key=operator.itemgetter(0)))

        # dumping directly to a file is somewhat unsafe. If the `Variable`/`Type` data ends up in a
        # state that cannot be serialized correctly, the json ends up malformed, and we could end up
        # overwriting valid test data with a complete mess. Since the in-memory data is typically
        # completely valid, the testing logic will pass and make it seem like nothing is wrong.

        # While we could rely on git to help revert the test file, it's better to just not allow it
        # to save malformed json in the first place. Thus, we dump the JSON, re-read it to check
        # for `JSONDecodeError`, and write it to the target file if no error occurred.
        x = json.dumps(clean_nones(asdict(self)), indent=" ")
        _ = json.loads(x)

        # ensure the necessary directories exist first
        import pathlib

        os.makedirs(pathlib.Path(path).parent, exist_ok=True)

        with open(path, "w", newline="\n") as f:
            f.write(x)
            f.write("\n")

    def print_json(self, metadata: BlessMetadata):
        """Prints `self` to `stdout` in json format with no formatting. This output is identicall to
        what would be saved via `TargetData.save_blessing`"""
        self.bless_metadata = metadata
        print(json.dumps(clean_nones(asdict(self))))


def clean_nones(value):
    """
    Recursively remove all None values from dictionaries and lists, and returns
    the result as a new dictionary or list.
    """
    if isinstance(value, list):
        x = [clean_nones(x) for x in value if x is not None]
        return x if len(x) != 0 else None
    elif isinstance(value, dict):
        x = {
            key: clean_nones(val)
            for key, val in value.items()
            if clean_nones(val) is not None
        }
        return x if len(x) != 0 else None
    else:
        return value


INPUT_DATA: TargetData = TargetData.initialize()


TYPES_TESTED: dict[str, Result] = {}
"""Since types are unique and unchanging, we only need to test each type once. This also helps
ensure we have tested all types in `INPUT_DATA`
"""


VARS_TESTED: list[dict[str, Result]] = []
"""Used to help ensure all expected variables were tested. Each element of the list corresponds to a
breakpoint, and contains a set of all of the variable names tested for that breakpoint."""


def tested_all_types() -> bool:
    """Returns true if all types in INPUT_DATA were tested this run."""

    expected_types = set(INPUT_DATA.types)
    untested_types = expected_types.difference(TYPES_TESTED.keys())

    if len(untested_types) != 0:
        print(
            f"{ANSI_RED}[repr error]{ANSI_END} The following types were expected, but were not \
tested:\n  {untested_types}"
        )

    return len(untested_types) == 0


def tested_all_variables() -> bool:
    expected_vars = [set(vars) for vars in INPUT_DATA.breakpoints]

    untested_vars = [
        expected.difference(tested.keys())
        for expected, tested in zip(expected_vars, VARS_TESTED)
    ]

    tested_not_expected = [
        set(tested.keys()).difference(expected)
        for expected, tested in zip(expected_vars, VARS_TESTED)
    ]

    result = True

    # `zip()` stops producing values once the shorter of the 2 iterators are exhausted, so we need
    # to check if we found more breakpoints than expected.
    #
    # If `expected_vars` were shorter, it would have caused an indexing error during the `check`
    # logic (which also outputs an error message and returns `Result.Mismatch`), so that case does
    # not need to be checked here.
    if len(VARS_TESTED) > len(expected_vars):
        result = False
        print(
            f"{ANSI_RED}[repr error]{ANSI_END} Expected data for {len(expected_vars)} \
breakpoints, got data for {len(VARS_TESTED)} breakpoints.w"
        )

    for i, v in enumerate(untested_vars):
        if len(v) == 0:
            continue

        result = False
        print(
            f"{ANSI_RED}[repr error]{ANSI_END} The following variables were expected at \
breakpoint#{i}, but were not tested:\n  {v}"
        )

    for i, v in enumerate(tested_not_expected):
        if len(v) == 0:
            continue

        result = False
        print(
            f"{ANSI_RED}[repr error]{ANSI_END} The following variables were tested, but do not \
exist in the input data at breakpoint#{i}:\n  {v}"
        )

    return result
