"""Contains the logic that compares variables to `INPUT_DATA` via the entrypoint
`check(var_name, breakpoint_idx, frame)`. These comparisons report errors to stdout, and then return
a `Result` indicating whether or not the variable matched.

Checks *do not* stop after the first encountered error. Some redundant information may be ommitted
(e.g. checking pretty printed type name if the synthetic isn't properly attached to the type).
"""

from typing import Any, Callable

import lldb

from ..common import (
    BLESS,
    INPUT_DATA,
    VARS_TESTED,
    DbgType,
    DbgValue,
    Result,
    Type,
    print_error,
)
from .from_lldb import (
    BasicType,
    TypeClass,
    bless_variable,
    get_generics,
    type_from_lldb,
    variable_from_lldb,
)


class LldbValue(DbgValue):
    __slots__ = ("child_list", "val")

    def __init__(self, valobj: lldb.SBValue):
        self.val = valobj
        self.child_list: list[DbgValue] = [
            LldbValue(self.val.GetChildAtIndex(i))
            for i in range(self.val.GetNumChildren())
        ]

    def type(self) -> DbgType:
        return LldbType(self.val.GetType(), self.val.GetTarget())

    def children(self) -> list[DbgValue]:
        return self.child_list

    def nth_child(self, n: int) -> DbgValue:
        return LldbValue(self.val.GetChildAtIndex(n))

    def name(self) -> str:
        return self.val.GetName()

    def error_hint(self, for_mismatch: str) -> str:
        summary_prov = (self.val.GetTypeSummary().GetData() or "").strip()
        synth_prov = (self.val.GetTypeSynthetic().GetData() or "").strip()

        return {
            "type": "SBValue.GetTypeName()",
            "summary": "SBValue.GetTypeSummary()",
            "pretty_print": f"{summary_prov}({self.name()}, {{}})",
            "value": f"{synth_prov}.get_value()",
            "format": "SBValue.GetTypeFormat()",
            "synthetic": "SBValue.GetTypeSynthetic()",
            "pretty_type_name": f"{synth_prov}.get_type_name()",
        }[for_mismatch]

    def invoke_summary_provider(self, import_path: str):
        provider = get_provider(import_path)
        provider(self.val, {})

    def invoke_synthetic_provider(self, import_path: str):
        provider = get_provider(import_path)

        # First we check for exceptions in the constructor and initialization
        synth: lldb.SBSyntheticValueProvider = provider(
            self.val.GetNonSyntheticValue(), {}
        )
        synth.update()

        # If the `get_child_at_index` function doesn't exist, there's not much more we
        # can do
        if getattr(synth, "get_child_at_index", None) is None:
            return

        # If all the children are invalid (e.g. because a template arg isn't
        # resolving correctly, incorrect enum discriminant), we should dump the
        # internal state of the synthetic
        if not all(
            synth.get_child_at_index(i).IsValid() for i in range(synth.num_children())
        ):
            dump_synthetic_state(synth)


class LldbType(DbgType):
    __slots__ = ("target", "ty")

    def __init__(self, ty: lldb.SBType, target: lldb.SBTarget):
        self.ty = ty
        self.target = target

    def name(self) -> str:
        return self.ty.GetName()

    def type_from_dbg(self) -> Type:
        return type_from_lldb(self.ty, self.target)

    def field_types(self) -> list[DbgType]:
        return [LldbType(field.GetType(), self.target) for field in self.ty.fields]

    def generics(self) -> list[DbgType]:
        return [
            LldbType(generic, self.target)
            for generic in get_generics(self.ty, self.target)
        ]

    def basic_type(self, raw: int | None) -> str:
        if raw is None:
            return "<None>"

        return BasicType(raw).name

    def type_class(self, raw: int | None) -> str:
        if raw is None:
            return "<None>"

        return TypeClass(raw).name or "<None>"


def check(var_name: str, breakpoint_idx: int, frame: lldb.SBFrame) -> Result:
    """`lldb-repr` pseudo-command entrypoint. Checks the variable against `INPUT_DATA` for the given
    frame at the given breakpoint.
    """

    if BLESS:
        print(f"blessing var {var_name}")
        bless_variable(INPUT_DATA, var_name, breakpoint_idx, frame)

    # Even if we're blessing, we still want to run the variable through the test to make sure we're
    # not somehow saving invalid information

    valobj: lldb.SBValue = frame.var(var_name)
    if not valobj.IsValid():
        print_error(var_name, "Unable to find variable")
        return Result.Mismatch

    var = variable_from_lldb(valobj)

    try:
        expected = INPUT_DATA.breakpoints[breakpoint_idx][var_name]
    except IndexError:
        print_error("INPUT_DATA", f"No data found for breakpoint #{breakpoint_idx}")
        return Result.Mismatch
    except KeyError:
        print_error(
            "INPUT_DATA",
            f"No data found for var '{var_name}' at breakpoint #{breakpoint_idx}",
        )
        return Result.Mismatch

    result = var.matches_expected(expected, LldbValue(valobj))
    # --bless outputs blank breakpoints for any breakpoints with no variables, so we need to account
    # for that here
    if len(VARS_TESTED) <= breakpoint_idx:
        VARS_TESTED.extend({} for _ in range(1 + breakpoint_idx - len(VARS_TESTED)))

    VARS_TESTED[breakpoint_idx][var_name] = result

    if result == Result.Ok:
        print(f"{var_name}: Ok")

    return result


def dump_synthetic_state(synth: Any):
    """Prints an object via builtin `vars()`. If `obj.__dict__` does not exist because the object is
    using `__slots__` intsead, the `__slots__` are converted into a dict and printed."""
    if (getattr(synth, "__dict__", None)) is not None:
        fields = vars(synth)
    elif (slots := getattr(synth, "__slots__", None)) is not None:
        fields = {name: getattr(synth, name, None) for name in slots}
    else:
        # Shouldn't be possible, but better safe than sorry
        print("Unable to print Synthetic Provider state")
        return

    print(f"Synthetic Provider state:\n  {fields})")


def get_provider(provider_str: str) -> Callable[[lldb.SBValue, dict[Any, Any]], Any]:
    """Given a Varible.summary or Variable.Synthetic, imports the appropriate module and returns the
    matching Class/Function"""
    import importlib

    [module, summary_name] = provider_str.split(".", 1)
    provider_module = importlib.import_module(module)

    return getattr(provider_module, summary_name)
