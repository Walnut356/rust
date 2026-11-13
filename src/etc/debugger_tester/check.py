import sys
import traceback
from functools import reduce

from .common import (
    INPUT_DATA,
    TYPES_TESTED,
    ArrayChild,
    ArrayLikeChildren,
    Child,
    DbgType,
    DbgValue,
    Result,
    Variable,
    print_error,
    print_mismatch,
)


def var_matches(var: Variable, expected: Variable, dbg_val: DbgValue) -> Result:
    # Happy path requires very little intercession from us. We keep these values on the stack
    # so we don't have to recalculate them if we need to do error handling
    summary_ok = var.summary == expected.summary
    synthetic_ok = var.synthetic == expected.synthetic
    pretty_type_name_ok = var.pretty_type_name == expected.pretty_type_name
    pretty_print_ok = var.pretty_print == expected.pretty_print
    format_ok = var.format == expected.format

    all_providers_ok = (
        summary_ok & synthetic_ok & format_ok & pretty_type_name_ok & pretty_print_ok
    )

    type_ok = var.type == expected.type

    if var.has_visualizer() or expected.has_visualizer():
        type_match_ok = type_matches(dbg_val.type(), all_providers_ok)
    else:
        type_match_ok = Result.Ok

    # (small hack) If the type information doesn't match, BUT it doesn't affect the synthetics,
    # we don't actually care that much. This prevents CI from failing when 2 different targets
    # use 2 different layouts, e.g.:
    # x86_64-linux-gnu: `Vec { buf, len, marker}`
    # aarch64-linux-gnu: `Vec {len, buf, marker}`
    if type_match_ok == Result.Mismatch and all_providers_ok:
        type_match_ok = Result.Ok

    value_ok = var.value == expected.value

    work_list = []
    work_list.extend(dbg_val.children())
    child_types_ok = Result.Ok

    while len(work_list) != 0:
        obj = work_list.pop()

        work_list.extend(obj.children())

        # similar to type matching, we don't actually care if there's a mismatch if the
        # providers aren't affected. We still want to report it though just in case.
        if var.has_visualizer() or expected.has_visualizer():
            child_types_ok &= type_matches(dbg_val.type(), all_providers_ok)
            if child_types_ok == Result.Mismatch and all_providers_ok:
                child_types_ok = Result.Ok
        else:
            child_types_ok = Result.Ok

    children_ok = children_match(
        var.children, expected.children, dbg_val.name(), dbg_val
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
            var.type,
            expected.type,
        )

    # We check the summary next since it's the most user-visible output. We don't need to check
    # `pretty_print` if the summary provider doesn't match.
    if not summary_ok:
        print_mismatch(
            error_source,
            f"summary ({dbg_val.error_hint('summary')})",
            var.summary,
            expected.summary,
        )
    elif not pretty_print_ok:
        print_mismatch(
            error_source,
            f"pretty_print ({dbg_val.error_hint('pretty_print')})",
            var.pretty_print,
            expected.pretty_print,
        )

        # try the summary provider directly to see if it's throwing an exception
        if var.summary is not None:
            try:
                dbg_val.invoke_summary_provider(var.summary)
            except Exception as e:  # noqa: BLE001
                print_error(
                    error_source + " Summary",
                    "Error while running Summary Provider:",
                )
                traceback.print_exception(type(e), e, e.__traceback__, file=sys.stdout)

    # Next we check the value and formatter. These mostly affect primitives.
    if not value_ok:
        print_mismatch(
            error_source,
            f"value ({dbg_val.error_hint('value')})",
            var.value,
            expected.value,
        )
    if not format_ok:
        print_mismatch(
            error_source,
            f"format ({dbg_val.error_hint('format')})",
            var.format,
            expected.format,
        )

    # Synthetic is checked next since children, pretty type name, and pretty print rely on it.
    # If the synthetic doesn't match, we can assume those won't match either.
    if not synthetic_ok:
        print_mismatch(
            error_source,
            f"synthetic ({dbg_val.error_hint('synthetic')})",
            var.synthetic,
            expected.synthetic,
        )
    else:
        if not pretty_type_name_ok:
            print_mismatch(
                error_source,
                f"pretty_type_name ({dbg_val.error_hint('pretty_type_name')})",
                var.pretty_type_name,
                expected.pretty_type_name,
            )

        if not children_ok and var.synthetic is not None:
            # If the children don't match, we can check for more catastrophic failures using the
            # synthetic provider. All the per-children errors will have been printed in the
            # `children_match` check above.
            try:
                dbg_val.invoke_synthetic_provider(var.synthetic)
            except Exception as e:  # noqa: BLE001
                print_error(
                    error_source + " Synthetic",
                    "Error while running Synthetic Provider:",
                )
                traceback.print_exception(type(e), e, e.__traceback__, file=sys.stdout)

    return Result.Mismatch


def type_matches(dbg_ty: DbgType, provider_ok: bool = False) -> Result:
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
        (type_matches(t, provider_ok) for t in dbg_ty.field_types()),
        result,
    )

    result = reduce(
        lambda x, y: x and y,
        (type_matches(t, provider_ok) for t in dbg_ty.generics()),
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
            result &= children_match(
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
            f"{got.name}: {got.type} = {got.value}" for got in children[len(expected) :]
        )
        print_error(
            path,
            f"The following children were found, but were not expected:\n    {error_str}",
        )

    return result
