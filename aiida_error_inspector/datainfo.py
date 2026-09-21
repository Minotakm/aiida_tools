"""Human-readable summaries of AiiDA data nodes.

Structures get a full treatment — formula, cell, volume, density, sites — since
in a high-throughput campaign the structure is the subject of the whole
calculation and "which material failed?" is usually the first question. Other
data nodes get a sensible generic summary so every node in the provenance tree
is inspectable rather than opaque.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

MAX_SITES = 60
MAX_DICT_KEYS = 80


def describe(node) -> str:
    """Dispatch on node type and return a plain-text panel body."""
    try:
        if _is(node, "StructureData"):
            return _describe_structure(node)
        if _is(node, "Dict"):
            return _describe_dict(node)
        if _is(node, "KpointsData"):
            return _describe_kpoints(node)
        if _is(node, "ArrayData", "BandsData", "XyData", "TrajectoryData"):
            return _describe_array(node)
        if _is(node, "RemoteData"):
            return _describe_remote(node)
        if _is(node, "FolderData", "SinglefileData"):
            return _describe_folder(node)
        if _is(node, "UpfData", "PseudoPotentialData"):
            return _describe_pseudo(node)
        return _describe_generic(node)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Describing node %s failed", getattr(node, "pk", "?"))
        return f"{_header(node)}\n\n[could not summarise this node: {exc}]"


def _is(node, *names: str) -> bool:
    mro = {cls.__name__ for cls in type(node).__mro__}
    return bool(mro & set(names))


def _header(node) -> str:
    kind = type(node).__name__
    lines = [
        f"PK {node.pk}   UUID {node.uuid}",
        f"Type       {kind}",
    ]
    if getattr(node, "label", None):
        lines.append(f"Label      {node.label}")
    if getattr(node, "description", None):
        lines.append(f"Note       {node.description}")
    try:
        lines.append(f"Created    {node.ctime:%Y-%m-%d %H:%M:%S}")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


def _extras_block(node) -> list[str]:
    """Extras often carry the provenance that matters — source database IDs."""
    try:
        extras = dict(node.base.extras.all)
    except Exception:  # noqa: BLE001
        return []
    extras.pop("_aiida_hash", None)
    if not extras:
        return []
    out = ["", "Extras", "-" * 68]
    for key in sorted(extras):
        value = str(extras[key])
        if len(value) > 90:
            value = value[:87] + "…"
        out.append(f"  {key:<26} {value}")
    return out


# --------------------------------------------------------------------------- #
# Structures
# --------------------------------------------------------------------------- #


def _describe_structure(node) -> str:
    lines = [_header(node), ""]

    try:
        lines.append(f"Formula    {node.get_formula()}")
    except Exception:  # noqa: BLE001
        pass
    try:
        lines.append(f"Hill       {node.get_formula(mode='hill')}")
    except Exception:  # noqa: BLE001
        pass

    try:
        sites = node.sites
        kinds = node.kinds
        lines.append(f"Sites      {len(sites)} atoms, {len(kinds)} kind(s)")
        symbols = sorted(node.get_symbols_set())
        lines.append(f"Elements   {', '.join(symbols)}")
    except Exception:  # noqa: BLE001
        sites, kinds = [], []

    try:
        volume = node.get_cell_volume()
        lines.append(f"Volume     {volume:.3f} Å³")
        if sites:
            lines.append(f"           {volume / len(sites):.3f} Å³/atom")
        mass = _total_mass(node)
        if mass:
            # 1 amu/Å³ = 1.66053906660 g/cm³
            lines.append(f"Density    {mass / volume * 1.66053906660:.3f} g/cm³")
    except Exception:  # noqa: BLE001
        pass

    try:
        lines.append(f"PBC        {node.pbc}")
    except Exception:  # noqa: BLE001
        pass

    spacegroup = _spacegroup(node)
    if spacegroup:
        lines.append(f"Spacegroup {spacegroup}")

    try:
        lengths = node.cell_lengths
        angles = node.cell_angles
        lines += [
            "",
            "Cell",
            "-" * 68,
            "  a, b, c    " + "  ".join(f"{v:10.5f}" for v in lengths),
            "  α, β, γ    " + "  ".join(f"{v:10.5f}" for v in angles),
            "",
            "  vectors",
        ]
        for vector in node.cell:
            lines.append("    " + "  ".join(f"{v:11.6f}" for v in vector))
    except Exception:  # noqa: BLE001
        pass

    if sites:
        lines += ["", f"Sites ({len(sites)})", "-" * 68]
        lines.append(f"  {'kind':<10}{'x':>12}{'y':>12}{'z':>12}")
        for site in sites[:MAX_SITES]:
            x, y, z = site.position
            lines.append(f"  {site.kind_name:<10}{x:12.6f}{y:12.6f}{z:12.6f}")
        if len(sites) > MAX_SITES:
            lines.append(f"  … and {len(sites) - MAX_SITES} more")

    lines += _extras_block(node)
    return "\n".join(lines)


def _total_mass(node) -> float | None:
    try:
        masses = {kind.name: kind.mass for kind in node.kinds}
        return sum(masses[site.kind_name] for site in node.sites)
    except Exception:  # noqa: BLE001
        return None


def _spacegroup(node) -> str | None:
    """Symmetry, when spglib happens to be installed. Optional by design."""
    try:
        import spglib  # noqa: PLC0415
    except ImportError:
        return None
    try:
        cell = (
            node.cell,
            [site.position for site in node.sites],
            [
                sorted({k.name for k in node.kinds}).index(site.kind_name)
                for site in node.sites
            ],
        )
        # spglib wants scaled positions; convert.
        import numpy as np  # noqa: PLC0415

        inverse = np.linalg.inv(np.array(node.cell)).T
        scaled = [inverse.dot(np.array(p)) for p in cell[1]]
        dataset = spglib.get_symmetry_dataset((node.cell, scaled, cell[2]), symprec=1e-5)
        if dataset is None:
            return None
        number = dataset["number"] if isinstance(dataset, dict) else dataset.number
        symbol = (
            dataset["international"]
            if isinstance(dataset, dict)
            else dataset.international
        )
        return f"{symbol} (#{number})"
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Other data types
# --------------------------------------------------------------------------- #


def _describe_dict(node) -> str:
    lines = [_header(node), ""]
    try:
        payload = node.get_dict()
    except Exception as exc:  # noqa: BLE001
        return "\n".join(lines + [f"[could not read: {exc}]"])
    lines += [f"Keys       {len(payload)}", "", "Contents", "-" * 68]
    lines += _render_mapping(payload)
    lines += _extras_block(node)
    return "\n".join(lines)


def _render_mapping(payload: dict, indent: int = 2, depth: int = 0) -> list[str]:
    out: list[str] = []
    pad = " " * indent
    for index, key in enumerate(sorted(payload, key=str)):
        if index >= MAX_DICT_KEYS:
            out.append(f"{pad}… and {len(payload) - MAX_DICT_KEYS} more keys")
            break
        value = payload[key]
        if isinstance(value, dict) and depth < 2:
            out.append(f"{pad}{key}:")
            out += _render_mapping(value, indent + 2, depth + 1)
        else:
            text = str(value)
            if len(text) > 80:
                text = text[:77] + "…"
            out.append(f"{pad}{str(key):<28} {text}")
    return out


def _describe_kpoints(node) -> str:
    lines = [_header(node), ""]
    try:
        mesh, offset = node.get_kpoints_mesh()
        lines.append(f"Mesh       {mesh[0]} x {mesh[1]} x {mesh[2]}")
        lines.append(f"Offset     {offset}")
    except Exception:  # noqa: BLE001
        try:
            points = node.get_kpoints()
            lines.append(f"Explicit   {len(points)} k-points")
            for point in points[:20]:
                lines.append("    " + "  ".join(f"{v:10.6f}" for v in point))
            if len(points) > 20:
                lines.append(f"    … and {len(points) - 20} more")
        except Exception:  # noqa: BLE001
            lines.append("[no mesh or explicit list stored]")
    lines += _extras_block(node)
    return "\n".join(lines)


def _describe_array(node) -> str:
    lines = [_header(node), ""]
    try:
        names = list(node.get_arraynames())
    except Exception:  # noqa: BLE001
        names = []
    if names:
        lines += [f"Arrays     {len(names)}", "", "Name / shape / dtype", "-" * 68]
        for name in names:
            try:
                array = node.get_array(name)
                lines.append(f"  {name:<28} {str(array.shape):<18} {array.dtype}")
            except Exception:  # noqa: BLE001
                lines.append(f"  {name:<28} [unreadable]")
    else:
        lines.append("[no arrays stored]")
    lines += _extras_block(node)
    return "\n".join(lines)


def _describe_remote(node) -> str:
    lines = [_header(node), ""]
    try:
        lines.append(f"Path       {node.get_remote_path()}")
    except Exception:  # noqa: BLE001
        pass
    try:
        computer = node.computer
        if computer is not None:
            lines.append(f"Computer   {computer.label}")
    except Exception:  # noqa: BLE001
        pass
    try:
        lines += ["", "Remote contents", "-" * 68]
        for name in sorted(node.listdir())[:60]:
            lines.append(f"  {name}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  [not reachable: {exc}]")
    lines += _extras_block(node)
    return "\n".join(lines)


def _describe_folder(node) -> str:
    lines = [_header(node), ""]
    try:
        names = node.base.repository.list_object_names()
        lines += [f"Files      {len(names)}", "", "Contents", "-" * 68]
        for name in sorted(names)[:80]:
            lines.append(f"  {name}")
        if len(names) > 80:
            lines.append(f"  … and {len(names) - 80} more")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"[could not list: {exc}]")
    lines += _extras_block(node)
    return "\n".join(lines)


def _describe_pseudo(node) -> str:
    lines = [_header(node), ""]
    for attribute in ("element", "md5"):
        value = getattr(node, attribute, None)
        if value:
            lines.append(f"{attribute.capitalize():<10} {value}")
    try:
        names = node.base.repository.list_object_names()
        lines.append(f"File       {', '.join(names)}")
    except Exception:  # noqa: BLE001
        pass
    lines += _extras_block(node)
    return "\n".join(lines)


def _describe_generic(node) -> str:
    lines = [_header(node), ""]
    value = getattr(node, "value", None)
    if value is not None:
        lines.append(f"Value      {value}")
    try:
        attributes: dict[str, Any] = dict(node.base.attributes.all)
    except Exception:  # noqa: BLE001
        attributes = {}
    if attributes:
        lines += ["", "Attributes", "-" * 68]
        lines += _render_mapping(attributes)
    lines += _extras_block(node)
    return "\n".join(lines)
