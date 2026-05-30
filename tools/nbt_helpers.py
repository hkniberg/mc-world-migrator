"""Shared NBT pretty-printing helpers."""
from nbt.nbt import TAG_Compound, TAG_List, NBTFile


def nbt_to_text(tag, indent=0, max_depth=12):
    if indent > max_depth:
        return '  ' * indent + '...'
    pad = '  ' * indent
    if isinstance(tag, (TAG_Compound, NBTFile)):
        out = []
        try:
            for k in tag.keys():
                v = tag[k]
                vt = type(v).__name__
                if isinstance(v, (TAG_Compound, TAG_List)):
                    out.append(f"{pad}{k} ({vt}):")
                    out.append(nbt_to_text(v, indent + 1, max_depth))
                else:
                    val = getattr(v, 'value', v)
                    if hasattr(val, '__len__') and not isinstance(val, str) and len(val) > 16:
                        val = f"<{type(val).__name__} len={len(val)}>"
                    out.append(f"{pad}{k} ({vt}): {val}")
        except Exception as e:
            out.append(f"{pad}<error reading compound: {e}>")
        return '\n'.join(out)
    elif isinstance(tag, TAG_List):
        out = []
        for i, item in enumerate(tag):
            if i > 50:
                out.append(f"{pad}... ({len(tag) - 50} more)")
                break
            out.append(f"{pad}[{i}]:")
            out.append(nbt_to_text(item, indent + 1, max_depth))
        return '\n'.join(out)
    else:
        return f"{pad}{getattr(tag, 'value', tag)}"
