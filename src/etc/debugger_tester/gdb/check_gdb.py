import gdb

from ..check import var_matches
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
from .from_gdb import (
    TypeCode,
    bless_variable,
    get_breakpoint_idx,
    get_children,
    get_template_args,
    get_type_name,
    make_visualizer,
    type_from_gdb,
    variable_from_gdb,
)


class GdbValue(DbgValue):
    __slots__ = ("child_list", "value", "var_name")

    def __init__(self, value: gdb.Value, var_name: str):
        self.value = value
        self.var_name = var_name
        self.child_list: list[DbgValue] = [
            GdbValue(child, name)
            for (name, child) in get_children(make_visualizer(value))
        ]

    def type(self) -> DbgType:
        return GdbType(self.value.type)

    def children(self) -> list[DbgValue]:
        return self.child_list

    def name(self) -> str:
        return self.var_name

    def error_hint(self, for_mismatch: str) -> str: ...
    def invoke_summary_provider(self, import_path: str = ""):
        provider = make_visualizer(self.value)
        _ = provider.to_string()

    def invoke_synthetic_provider(self, import_path: str):
        # FIXME(Walnut356) at the moment I haven't fiddled with GDB enough to know what the
        # common failure states are for their pretty printers, so at the moment this check
        # is pretty barebones
        _provider = make_visualizer(self.value)


class GdbType(DbgType):
    __slots__ = ("ty",)

    def __init__(self, ty: gdb.Type):
        self.ty: gdb.Type = ty

    def name(self) -> str:
        return get_type_name(self.ty)

    def type_from_dbg(self) -> Type:
        return type_from_gdb(self.ty)

    def field_types(self) -> list[DbgType]:
        return [GdbType(f.type) for f in self.ty.fields()]

    def generics(self) -> list[DbgType]:
        return [GdbType(t) for t in get_template_args(self.ty)]

    def basic_type(self, raw: int | None) -> str:
        return str(raw)

    def type_class(self, raw: int | None) -> str:
        if raw is None:
            return "<None>"
        return TypeCode(self.ty.code).name


def check(var_name: str):
    if BLESS:
        print(f"blessing var {var_name}")
        bless_variable(var_name)

    # Even if we're blessing, we still want to run the variable through the test to make sure we're
    # not somehow saving invalid information

    valobj = gdb.selected_frame().read_var(var_name)

    var = variable_from_gdb(valobj)

    breakpoint_idx = get_breakpoint_idx()

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

    result = var_matches(var, expected, GdbValue(valobj, var_name))
    # --bless outputs blank breakpoints for any breakpoints with no variables, so we need to account
    # for that here
    if len(VARS_TESTED) <= breakpoint_idx:
        VARS_TESTED.extend({} for _ in range(1 + breakpoint_idx - len(VARS_TESTED)))

    VARS_TESTED[breakpoint_idx][var_name] = result

    if result == Result.Ok:
        print(f"{var_name}: Ok")

    return result
