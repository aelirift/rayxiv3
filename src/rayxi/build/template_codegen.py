from __future__ import annotations

from collections.abc import Iterable, Sequence


def _property_names_by_owner(slice_data: dict) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}

    for prop_id in (slice_data.get("property_details") or {}).keys():
        if not isinstance(prop_id, str) or "." not in prop_id:
            continue
        owner, name = prop_id.split(".", 1)
        owners.setdefault(owner, set()).add(name)

    for node in slice_data.get("owned_properties") or []:
        prop_id = node.get("id") or node.get("name")
        if not isinstance(prop_id, str) or "." not in prop_id:
            continue
        owner, name = prop_id.split(".", 1)
        owners.setdefault(owner, set()).add(name)

    def _add_ref(ref: str) -> None:
        if not isinstance(ref, str) or "." not in ref:
            return
        owner, name = ref.split(".", 1)
        owners.setdefault(owner, set()).add(name)

    for edge in slice_data.get("own_reads") or []:
        _add_ref(edge.get("source", ""))
    for edge in slice_data.get("own_writes") or []:
        _add_ref(edge.get("target", ""))

    return owners


def resolve_property_name(
    slice_data: dict,
    owner: str,
    candidates: Sequence[str],
    *,
    default: str | None = None,
) -> str:
    owner_props = _property_names_by_owner(slice_data).get(owner, set())
    for name in candidates:
        if name in owner_props:
            return name
    if default is not None:
        return default
    return candidates[0]


def resolve_constant(
    constants: dict | None,
    candidates: Iterable[str],
    default,
):
    if isinstance(constants, dict):
        for name in candidates:
            if name in constants and constants[name] is not None:
                return constants[name]
    return default
