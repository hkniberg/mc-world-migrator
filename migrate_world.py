"""Repair a 1.20.1 (Forge) world that was opened in 1.21.1 (NeoForge).

Reads the original 1.20 world as the source of truth and rewrites NBT in the
migrated 1.21 world that vanilla DataFixerUpper / mod migration dropped or
corrupted (mod-stored item counts, backpacks, fluid tanks, factory-gauge links,
shops, clipboards, paintings, tracks, filters, complex item components, ...).

See README.md for what is and isn't fixed, and the recommended workflow.

Usage (dry-run by default; always work on a COPY):
    # parallel, resumable migration of the whole world (recommended)
    python migrate_world.py run --source <1.20_world> --target <migrated_copy> --jobs 20 [--write]

    # a single region (testing / targeted rerun)
    python migrate_world.py region --coord overworld.3.3 --source <1.20> --target <copy> [--write]

`run` reads each chunk once and processes regions in parallel, then does one global
.dat/playerdata pass; it writes a migration_manifest.json so a crashed run resumes
where it left off. `fix-all` is the single-threaded equivalent (fine for tiny
worlds). Run `python migrate_world.py -h` for all subcommands. Idempotent.
See README.md for what is and isn't fixed.
"""
import argparse
import sys
import os
import re
import json
import random
import shutil
import struct
import time
import traceback
import uuid
from pathlib import Path
from collections import defaultdict, Counter
from concurrent.futures import ProcessPoolExecutor, as_completed

from nbt import region, nbt as nbt_mod
from nbt.nbt import (
    TAG_Compound, TAG_List, TAG_String, TAG_Byte, TAG_Int, TAG_Long,
    TAG_Int_Array, TAG_Float, TAG_Double, TAG_Short, NBTFile,
    TAG_Byte_Array, TAG_Long_Array, TAGLIST,
)

# --- Minecraft NBT strings use Java "modified UTF-8" (CESU-8): NUL is encoded as
# 0xC0 0x80 and supplementary chars as 6-byte surrogate pairs (0xED prefixes).
# The `nbt` library uses plain UTF-8 and crashes on such strings (e.g. some
# backpack/item names in sophisticatedbackpacks.dat). Patch TAG_String to use
# modified UTF-8 so those .dat files both load and round-trip correctly. ---
from mutf8 import decode_modified_utf8 as _decode_mutf8, encode_modified_utf8 as _encode_mutf8
from nbt.nbt import StructError as _StructError

def _tag_string_parse_buffer(self, buffer):
    length = TAG_Short(buffer=buffer)
    read = buffer.read(length.value)
    if len(read) != length.value:
        raise _StructError()
    try:
        self.value = read.decode("utf-8")          # fast path (BMP / ASCII)
    except UnicodeDecodeError:
        self.value = _decode_mutf8(read)            # modified UTF-8 (NUL / supplementary)

def _tag_string_render_buffer(self, buffer):
    try:
        save_val = _encode_mutf8(self.value)        # always emit modified UTF-8
    except Exception:
        save_val = self.value.encode("utf-8")
    length = TAG_Short(len(save_val))
    length._render_buffer(buffer)
    buffer.write(save_val)

TAG_String._parse_buffer = _tag_string_parse_buffer
TAG_String._render_buffer = _tag_string_render_buffer


# ============================================================================
# NBT helpers
# ============================================================================
def cget(c, name):
    """Get child tag of a TAG_Compound by name, or None."""
    if c is None:
        return None
    tags = getattr(c, 'tags', None)
    if tags is None:
        return None
    for t in tags:
        if t.name == name:
            return t
    return None


def cset(c, name, new_tag):
    """Replace (or insert) a child tag by name. Returns the new tag."""
    new_tag.name = name
    c.tags = [t for t in c.tags if t.name != name]
    c.tags.append(new_tag)
    return new_tag


def cdel(c, name):
    """Delete a child tag by name (no-op if absent)."""
    c.tags = [t for t in c.tags if t.name != name]


def has(c, name):
    return cget(c, name) is not None


def make_ia(values, name=''):
    t = TAG_Int_Array(name=name)
    t.value = [int(v) for v in values]
    return t


def make_int(value, name=''):
    t = TAG_Int(name=name)
    t.value = int(value)
    return t


def make_byte(value, name=''):
    t = TAG_Byte(name=name)
    t.value = int(value)
    return t


def make_string(value, name=''):
    t = TAG_String(name=name)
    t.value = str(value)
    return t


def make_double(value, name=''):
    t = TAG_Double(name=name)
    t.value = float(value)
    return t


def make_float(value, name=''):
    t = TAG_Float(name=name)
    t.value = float(value)
    return t


def make_short(value, name=''):
    t = TAG_Short(name=name)
    t.value = int(value)
    return t


def make_compound(name=''):
    c = TAG_Compound()
    c.name = name
    c.tags = []
    return c


def make_list(item_type, name=''):
    lst = TAG_List(name=name, type=item_type)
    lst.tags = []
    return lst


def xyz_compound_to_ia(c, name=''):
    """Convert a {X:Int, Y:Int, Z:Int} compound to an Int_Array[X,Y,Z]."""
    x = cget(c, 'X').value
    y = cget(c, 'Y').value
    z = cget(c, 'Z').value
    return make_ia([x, y, z], name=name)


# ============================================================================
# Sources index
# ============================================================================
class Sources:
    """Index the 1.20 source world for fast lookup."""

    def __init__(self, world_dir):
        self.world = Path(world_dir)
        self.be_by_xyz = {}   # (mc_id, x, y, z) -> BE compound
        self.ent_by_uuid = {} # tuple(uuid_int_array) -> entity compound
        self.playerdata = {}  # filename -> NBTFile
        self._load()

    def _load(self):
        rdir = self.world / 'region'
        if rdir.exists():
            for f in sorted(rdir.glob('*.mca')):
                try:
                    rf = region.RegionFile(str(f))
                except Exception:
                    continue
                for entry in rf.get_chunk_coords():
                    try:
                        chunk = rf.get_chunk(entry['x'], entry['z'])
                    except Exception:
                        continue
                    if chunk is None:
                        continue
                    bes = cget(chunk, 'block_entities')
                    if bes is None:
                        continue
                    for be in bes.tags:
                        tid = cget(be, 'id')
                        x = cget(be, 'x')
                        y = cget(be, 'y')
                        z = cget(be, 'z')
                        if tid and x and y and z:
                            self.be_by_xyz[(tid.value, x.value, y.value, z.value)] = be
        edir = self.world / 'entities'
        if edir.exists():
            for f in sorted(edir.glob('*.mca')):
                try:
                    rf = region.RegionFile(str(f))
                except Exception:
                    continue
                for entry in rf.get_chunk_coords():
                    try:
                        chunk = rf.get_chunk(entry['x'], entry['z'])
                    except Exception:
                        continue
                    if chunk is None:
                        continue
                    ents = cget(chunk, 'Entities')
                    if ents is None:
                        continue
                    for ent in ents.tags:
                        u = cget(ent, 'UUID')
                        if u:
                            self.ent_by_uuid[tuple(u.value)] = ent
        pd = self.world / 'playerdata'
        if pd.exists():
            for f in sorted(pd.glob('*.dat')):
                try:
                    self.playerdata[f.name] = nbt_mod.NBTFile(str(f))
                except Exception:
                    pass
        print(f"[Sources] {len(self.be_by_xyz)} BEs, {len(self.ent_by_uuid)} entities, "
              f"{len(self.playerdata)} playerdata from {self.world}", file=sys.stderr)

    def be(self, mc_id, x, y, z):
        return self.be_by_xyz.get((mc_id, x, y, z))

    def entity_by_uuid(self, uuid):
        return self.ent_by_uuid.get(tuple(uuid))

    def count_by_id(self, mc_id):
        return sum(1 for k in self.be_by_xyz if k[0] == mc_id)


# ============================================================================
# Region walker
# ============================================================================
def walk_region(target_dir, kind, visitor, dry_run):
    """Walk every chunk in target/<kind>/*.mca and call visitor(chunk, stats).
    If visitor returns True the chunk is rewritten.
    """
    base = Path(target_dir) / kind
    if not base.exists():
        print(f"  (no {kind}/ directory in target)")
        return
    chunks_modified = 0
    chunks_total = 0
    for f in sorted(base.glob('*.mca')):
        try:
            rf = region.RegionFile(str(f))
        except Exception as e:
            print(f"  !! open fail {f.name}: {e}")
            continue
        local_mod = 0
        for entry in rf.get_chunk_coords():
            try:
                chunk = rf.get_chunk(entry['x'], entry['z'])
            except Exception:
                continue
            if chunk is None:
                continue
            chunks_total += 1
            if visitor(chunk):
                local_mod += 1
                if not dry_run:
                    rf.write_chunk(entry['x'], entry['z'], chunk)
        if local_mod:
            chunks_modified += local_mod
            print(f"  {f.name}: {local_mod} chunks {'rewritten' if not dry_run else 'WOULD rewrite'}")
    print(f"  chunks scanned={chunks_total}, modified={chunks_modified}")


# ============================================================================
# Fixer: mod block-ID renames (chunk block palette, region/ + all dims)
# ============================================================================
# Some mods renamed block IDs between their 1.20 and 1.21 builds (e.g. Biomes
# O' Plenty's maple/autumn leaves). The force-upgrade preserves the old palette
# strings verbatim — DataFixerUpper has no fixers for mod blocks — but the live
# 1.21 game turns any ID it can't resolve into AIR the first time the chunk
# loads. Renaming the palette strings here (after force-upgrade, before the
# world is ever opened in-game) makes those blocks resolve and survive. It's a
# pure string swap: block positions, palette indices, Properties, and every
# other chunk field are untouched. Naturally idempotent (once renamed, the old
# ID is gone, so a rerun matches nothing). Extend the map as more dead IDs surface.
BLOCK_RENAMES = {
    'biomesoplenty:maple_leaves':         'biomesoplenty:red_maple_leaves',
    'biomesoplenty:orange_autumn_leaves': 'biomesoplenty:orange_maple_leaves',
    'biomesoplenty:yellow_autumn_leaves': 'biomesoplenty:yellow_maple_leaves',
}


def make_block_renames_visitor(stats):
    """Rewrite dead mod block IDs in each section's block_states.palette."""
    def visit(chunk):
        if not BLOCK_RENAMES:
            return False
        sections = cget(chunk, 'sections')
        if sections is None:
            return False
        changed = False
        for sec in sections.tags:
            palette = cget(cget(sec, 'block_states'), 'palette')
            if palette is None:
                continue
            for entry in palette.tags:
                nm = cget(entry, 'Name')
                if nm is not None and nm.value in BLOCK_RENAMES:
                    stats[f'block_renamed:{nm.value}'] += 1
                    nm.value = BLOCK_RENAMES[nm.value]
                    changed = True
        return changed

    return visit


def fix_block_renames(target, dry_run):
    """Standalone: rename dead mod block IDs in chunk palettes (debug / overworld)."""
    print("--- fix_block_renames (mod block palette IDs) ---")
    stats = Counter()
    walk_region(target, 'region', make_block_renames_visitor(stats), dry_run)
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


# ============================================================================
# Fixer: chain conveyors (block_entities, region/)
# ============================================================================
def make_chain_conveyors_visitor(sources, stats):
    def visit(chunk):
        bes = cget(chunk, 'block_entities')
        if bes is None:
            return False
        changed = False
        for be in bes.tags:
            tid = cget(be, 'id')
            if tid is None or tid.value != 'create:chain_conveyor':
                continue
            stats['target_total'] += 1
            x = cget(be, 'x').value
            y = cget(be, 'y').value
            z = cget(be, 'z').value
            src = sources.be('create:chain_conveyor', x, y, z)
            if src is None:
                stats['no_source_match'] += 1
                continue
            stats['source_match'] += 1

            # Check Connections — if non-empty already, skip (idempotent)
            tgt_conn = cget(be, 'Connections')
            src_conn = cget(src, 'Connections')
            if src_conn is None or len(src_conn.tags) == 0:
                stats['source_no_connections'] += 1
                continue
            if tgt_conn is not None and len(tgt_conn.tags) > 0:
                stats['already_fixed'] += 1
                continue

            # Build new Connections: list of Int_Array[3]
            new_conn = make_list(TAG_Int_Array, name='Connections')
            for entry in src_conn.tags:
                ia = xyz_compound_to_ia(entry)
                new_conn.tags.append(ia)
            cset(be, 'Connections', new_conn)
            stats['fixed'] += 1
            changed = True
        return changed

    return visit


def fix_chain_conveyors(target, sources, dry_run):
    print("--- fix_chain_conveyors ---")
    stats = Counter()
    walk_region(target, 'region', make_chain_conveyors_visitor(sources, stats), dry_run)
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


# ============================================================================
# Fixer: tracks (block_entities, region/)
# ============================================================================
def make_tracks_visitor(sources, stats):
    def visit(chunk):
        bes = cget(chunk, 'block_entities')
        if bes is None:
            return False
        changed = False
        for be in bes.tags:
            tid = cget(be, 'id')
            if tid is None or tid.value != 'create:track':
                continue
            stats['target_total'] += 1
            x = cget(be, 'x').value
            y = cget(be, 'y').value
            z = cget(be, 'z').value
            src = sources.be('create:track', x, y, z)
            if src is None:
                stats['no_source_match'] += 1
                continue
            stats['source_match'] += 1

            tgt_connections = cget(be, 'Connections')
            src_connections = cget(src, 'Connections')
            if tgt_connections is None or src_connections is None:
                stats['no_connections'] += 1
                continue

            # Walk pairs of Connections[i] in target/source.
            # In each connection, replace target's Positions[j].Pos with source's
            # Positions[j] {X,Y,Z}. Iterate in parallel.
            be_changed = False
            n_pairs = min(len(tgt_connections.tags), len(src_connections.tags))
            for i in range(n_pairs):
                tgt_c = tgt_connections.tags[i]
                src_c = src_connections.tags[i]
                tgt_positions = cget(tgt_c, 'Positions')
                src_positions = cget(src_c, 'Positions')
                if tgt_positions is None or src_positions is None:
                    continue
                n_pos = min(len(tgt_positions.tags), len(src_positions.tags))
                for j in range(n_pos):
                    src_pos = src_positions.tags[j]  # {X,Y,Z} compound in 1.20
                    tgt_pos = tgt_positions.tags[j]  # {Pos: Int_Array} compound in 1.21

                    # Get source X, Y, Z
                    sx = cget(src_pos, 'X')
                    sy = cget(src_pos, 'Y')
                    sz = cget(src_pos, 'Z')
                    if sx is None or sy is None or sz is None:
                        continue

                    # Get current target Pos array
                    cur_pos = cget(tgt_pos, 'Pos')
                    src_xyz = [sx.value, sy.value, sz.value]

                    # Only rewrite if zeroed or missing
                    if cur_pos is not None:
                        cur_val = list(cur_pos.value)
                        if cur_val == src_xyz:
                            continue  # already correct
                    new_pos = make_ia(src_xyz, name='Pos')
                    cset(tgt_pos, 'Pos', new_pos)
                    be_changed = True
                    stats['positions_fixed'] += 1

            if be_changed:
                stats['fixed_be'] += 1
                changed = True
            else:
                stats['unchanged_be'] += 1
        return changed

    return visit


def fix_tracks(target, sources, dry_run):
    print("--- fix_tracks ---")
    stats = Counter()
    walk_region(target, 'region', make_tracks_visitor(sources, stats), dry_run)
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


# ============================================================================
# Fixer: track signals (block_entities, region/)
# ============================================================================
def make_track_signals_visitor(sources, stats):
    def visit(chunk):
        bes = cget(chunk, 'block_entities')
        if bes is None:
            return False
        changed = False
        for be in bes.tags:
            tid = cget(be, 'id')
            if tid is None or tid.value != 'create:track_signal':
                continue
            stats['target_total'] += 1
            x = cget(be, 'x').value
            y = cget(be, 'y').value
            z = cget(be, 'z').value
            src = sources.be('create:track_signal', x, y, z)
            if src is None:
                stats['no_source_match'] += 1
                continue
            stats['source_match'] += 1

            # Restore State if source has a non-INVALID one
            src_state = cget(src, 'State')
            tgt_state = cget(be, 'State')
            if src_state is not None and src_state.value not in ('INVALID',):
                if tgt_state is None or tgt_state.value != src_state.value:
                    cset(be, 'State', make_string(src_state.value))
                    stats['state_fixed'] += 1
                    changed = True

            # Restore Overlay if it differs
            src_overlay = cget(src, 'Overlay')
            tgt_overlay = cget(be, 'Overlay')
            if src_overlay is not None:
                if tgt_overlay is None or tgt_overlay.value != src_overlay.value:
                    cset(be, 'Overlay', make_string(src_overlay.value))
                    stats['overlay_fixed'] += 1
                    changed = True
        return changed

    return visit


def fix_track_signals(target, sources, dry_run):
    print("--- fix_track_signals ---")
    stats = Counter()
    walk_region(target, 'region', make_track_signals_visitor(sources, stats), dry_run)
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


# ============================================================================
# Fixer: paintings (entities, entities/)
# ============================================================================
# 1.20 immersive_paintings Facing enum (0=S,1=W,2=N,3=E,4=UP,5=DOWN) ->
# 1.21 vanilla Direction3D ordinal (DOWN0,UP1,NORTH2,SOUTH3,WEST4,EAST5).
# Keeping the raw value is what put paintings on the floor (raw 0 == DOWN).
_PAINTING_FACING = {0: 3, 1: 4, 2: 2, 3: 5, 4: 1, 5: 0}


def convert_painting_motive(m):
    """1.20 'immersive_paintings:paintings/foo-bar-32px.png' ->
    1.21 'immersive_paintings:datapack/<all non-alphanumerics stripped>'."""
    flat = re.sub(r'[^a-z0-9]', '', m.lower())
    return 'immersive_paintings:datapack/' + flat


def make_paintings_visitor(sources, stats):
    def visit(chunk):
        ents = cget(chunk, 'Entities')
        if ents is None:
            return False
        changed = False
        for ent in ents.tags:
            tid = cget(ent, 'id')
            if tid is None or tid.value != 'immersive_paintings:painting':
                continue
            stats['target_total'] += 1
            u = cget(ent, 'UUID')
            if u is None:
                stats['no_uuid'] += 1
                continue
            src = sources.entity_by_uuid(u.value)
            if src is None:
                stats['no_source_match'] += 1
                continue
            stats['source_match'] += 1

            # Restore Pos (list of 3 doubles) from source
            src_pos = cget(src, 'Pos')
            if src_pos is not None:
                src_xyz = [t.value for t in src_pos.tags]
                tgt_pos = cget(ent, 'Pos')
                tgt_xyz = [t.value for t in tgt_pos.tags] if tgt_pos is not None else None
                if src_xyz != tgt_xyz:
                    new_pos = make_list(TAG_Double, name='Pos')
                    for v in src_xyz:
                        new_pos.tags.append(make_double(v))
                    cset(ent, 'Pos', new_pos)
                    stats['pos_fixed'] += 1
                    changed = True

            # Remap Facing from the source's 1.20 value
            src_facing = cget(src, 'Facing')
            if src_facing is not None:
                want = _PAINTING_FACING.get(int(src_facing.value), int(src_facing.value))
                tgt_facing = cget(ent, 'Facing')
                if tgt_facing is None or not isinstance(tgt_facing, TAG_Int) or int(tgt_facing.value) != want:
                    cset(ent, 'Facing', make_int(want))
                    stats['facing_fixed'] += 1
                    changed = True

            # Convert Motive to the 1.21 datapack-flattened form
            src_motive = cget(src, 'Motive')
            if src_motive is not None:
                want_m = convert_painting_motive(src_motive.value)
                tgt_motive = cget(ent, 'Motive')
                if tgt_motive is None or tgt_motive.value != want_m:
                    cset(ent, 'Motive', make_string(want_m))
                    stats['motive_fixed'] += 1
                    changed = True
        return changed

    return visit


def fix_paintings(target, sources, dry_run):
    print("--- fix_paintings ---")
    stats = Counter()
    walk_region(target, 'entities', make_paintings_visitor(sources, stats), dry_run)
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


# ============================================================================
# Helper: build 1.21 computercraft:printout component from 1.20 tag
# ============================================================================
def build_printout_component(src_tag):
    """Return a 1.21 'computercraft:printout' compound built from a 1.20 tag.

    1.20 tag has Title (String), Pages (Int), and Text0..TextN, Color0..ColorN.
    Output: {title: String, lines: [{text: String, foreground: String}, ...]}
    """
    title = cget(src_tag, 'Title')
    pages = cget(src_tag, 'Pages')

    # Count the number of text fields present
    n_lines = 0
    for tag in src_tag.tags:
        if tag.name and tag.name.startswith('Text'):
            try:
                idx = int(tag.name[4:])
            except ValueError:
                continue
            if idx + 1 > n_lines:
                n_lines = idx + 1
    if n_lines == 0:
        return None

    component = make_compound()
    if title is not None:
        cset(component, 'title', make_string(title.value))
    lines_list = make_list(TAG_Compound, name='lines')
    for i in range(n_lines):
        text_tag = cget(src_tag, f'Text{i}')
        color_tag = cget(src_tag, f'Color{i}')
        text_val = text_tag.value if text_tag is not None else ''
        color_val = color_tag.value if color_tag is not None else 'f' * 25
        line = make_compound()
        cset(line, 'text', make_string(text_val))
        cset(line, 'foreground', make_string(color_val))
        lines_list.tags.append(line)
    cset(component, 'lines', lines_list)
    return component


# ============================================================================
# Fixer: item frames carrying broken CC printouts
# ============================================================================
PRINTOUT_IDS = (
    'computercraft:printed_page',
    'computercraft:printed_pages',
    'computercraft:printed_book',
)


def make_item_frames_visitor(sources, stats):
    def visit(chunk):
        ents = cget(chunk, 'Entities')
        if ents is None:
            return False
        changed = False
        for ent in ents.tags:
            tid = cget(ent, 'id')
            if tid is None or tid.value not in ('minecraft:item_frame', 'minecraft:glow_item_frame'):
                continue
            item = cget(ent, 'Item')
            if item is None:
                continue
            item_id = cget(item, 'id')
            if item_id is None or item_id.value not in PRINTOUT_IDS:
                continue
            stats['target_printout_frames'] += 1

            # Is it already populated?
            components = cget(item, 'components')
            if components is not None and cget(components, 'computercraft:printout') is not None:
                stats['already_populated'] += 1
                continue

            # Look up source by UUID
            u = cget(ent, 'UUID')
            if u is None:
                stats['no_uuid'] += 1
                continue
            src_ent = sources.entity_by_uuid(u.value)
            if src_ent is None:
                stats['no_source_match'] += 1
                continue
            src_item = cget(src_ent, 'Item')
            if src_item is None:
                stats['no_source_item'] += 1
                continue
            src_tag = cget(src_item, 'tag')
            if src_tag is None:
                stats['no_source_tag'] += 1
                continue

            printout = build_printout_component(src_tag)
            if printout is None:
                stats['empty_printout'] += 1
                continue

            if components is None:
                components = make_compound()
                cset(item, 'components', components)
            cset(components, 'computercraft:printout', printout)
            stats['fixed'] += 1
            changed = True
        return changed

    return visit


def fix_item_frames(target, sources, dry_run):
    print("--- fix_item_frames ---")
    stats = Counter()
    walk_region(target, 'entities', make_item_frames_visitor(sources, stats), dry_run)
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


# ============================================================================
# Helpers for backpack item structures
# ============================================================================
SCORE_KEY_MAP = {
    'contentsUuid':    'sophisticatedcore:storage_uuid',
    'inventorySlots':  'sophisticatedcore:number_of_inventory_slots',
    'upgradeSlots':    'sophisticatedcore:number_of_upgrade_slots',
    'renderInfo':      'sophisticatedcore:render_info_tag',
}


def promote_custom_data(item, force_uuid=None):
    """Promote a 1.20-style minecraft:custom_data payload up to proper
    sophisticatedcore:* components on a 1.21 item. If force_uuid is set
    (an Int_Array value), use it instead of custom_data.contentsUuid.
    Returns True if anything changed.
    """
    components = cget(item, 'components')
    if components is None:
        components = make_compound()
        cset(item, 'components', components)

    cd = cget(components, 'minecraft:custom_data')
    changed = False

    if force_uuid is not None:
        existing = cget(components, 'sophisticatedcore:storage_uuid')
        target_val = list(force_uuid)
        if existing is None or list(existing.value) != target_val:
            cset(components, 'sophisticatedcore:storage_uuid', make_ia(target_val))
            changed = True
    elif cd is not None:
        contents_uuid = cget(cd, 'contentsUuid')
        if contents_uuid is not None:
            existing = cget(components, 'sophisticatedcore:storage_uuid')
            target_val = list(contents_uuid.value)
            if existing is None or list(existing.value) != target_val:
                cset(components, 'sophisticatedcore:storage_uuid', make_ia(target_val))
                changed = True

    if cd is not None:
        slots = cget(cd, 'inventorySlots')
        if slots is not None and cget(components, 'sophisticatedcore:number_of_inventory_slots') is None:
            cset(components, 'sophisticatedcore:number_of_inventory_slots', make_int(slots.value))
            changed = True
        up = cget(cd, 'upgradeSlots')
        if up is not None and cget(components, 'sophisticatedcore:number_of_upgrade_slots') is None:
            cset(components, 'sophisticatedcore:number_of_upgrade_slots', make_int(up.value))
            changed = True
        ri = cget(cd, 'renderInfo')
        if ri is not None and cget(components, 'sophisticatedcore:render_info_tag') is None:
            new_ri = make_compound()
            new_ri.tags = list(ri.tags)
            cset(components, 'sophisticatedcore:render_info_tag', new_ri)
            changed = True

    return changed


def is_backpack(item_id):
    return item_id.startswith('sophisticatedbackpacks:') and 'backpack' in item_id


# ============================================================================
# Fixer: block-entity backpacks (region/) — restore storage_uuid from source
# ============================================================================
def make_be_backpacks_visitor(sources, stats):
    def visit(chunk):
        bes = cget(chunk, 'block_entities')
        if bes is None:
            return False
        changed = False
        for be in bes.tags:
            tid = cget(be, 'id')
            if tid is None or not is_backpack(tid.value):
                continue
            stats['target_total'] += 1
            x = cget(be, 'x').value
            y = cget(be, 'y').value
            z = cget(be, 'z').value
            src = sources.be(tid.value, x, y, z)
            if src is None:
                stats['no_source_match'] += 1
                continue
            stats['source_match'] += 1

            # Source BE has nested backpackData with 1.20 tag.contentsUuid
            src_data = cget(src, 'backpackData')
            if src_data is None:
                stats['no_source_data'] += 1
                continue
            src_tag = cget(src_data, 'tag')
            if src_tag is None:
                stats['no_source_tag'] += 1
                continue
            src_uuid = cget(src_tag, 'contentsUuid')
            if src_uuid is None:
                stats['no_source_uuid'] += 1
                continue

            # Target BE has backpackData with components.sophisticatedcore:storage_uuid
            tgt_data = cget(be, 'backpackData')
            if tgt_data is None:
                stats['no_target_data'] += 1
                continue

            if promote_custom_data(tgt_data, force_uuid=src_uuid.value):
                stats['fixed'] += 1
                changed = True
        return changed

    return visit


def fix_be_backpacks(target, sources, dry_run):
    print("--- fix_be_backpacks ---")
    stats = Counter()
    walk_region(target, 'region', make_be_backpacks_visitor(sources, stats), dry_run)
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


# ============================================================================
# Fixer: inventory backpacks (playerdata/) — promote custom_data.contentsUuid
# ============================================================================
def walk_items(tag, on_item):
    """Recurse through any compound looking for items (id+count or Count).
    Calls on_item(item_compound) for each."""
    if isinstance(tag, (TAG_Compound, NBTFile)):
        tid = cget(tag, 'id')
        has_count = has(tag, 'count') or has(tag, 'Count')
        if tid is not None and has_count and isinstance(tid.value, str):
            on_item(tag)
        for t in list(tag.tags):
            walk_items(t, on_item)
    elif isinstance(tag, TAG_List):
        for t in tag.tags:
            walk_items(t, on_item)


def fix_backpack_dat(target, dry_run):
    """Convert item stacks inside data/sophisticatedbackpacks.dat from 1.20
    (Count:Byte + realCount) to 1.21 (count:Int). The broken .dat retains full
    1.20 contents, so this is a self-contained in-place conversion."""
    print("--- fix_backpack_dat ---")
    path = Path(target) / 'data' / 'sophisticatedbackpacks.dat'
    if not path.exists():
        print("  (no sophisticatedbackpacks.dat)")
        return
    try:
        n = nbt_mod.NBTFile(str(path))
    except Exception as e:
        print(f"  !! open: {e}")
        return
    stats = Counter()

    def on_item(item):
        # 1.20-style stacks carry capital Count; convert in place.
        if cget(item, 'Count') is not None:
            conv = convert_item_1_20_to_1_21(item)  # id, count, components, Slot (drops realCount)
            item.tags = conv.tags
            stats['items_converted'] += 1

    walk_items(n, on_item)
    print(f"  items_converted: {stats['items_converted']}")
    if stats['items_converted'] and not dry_run:
        n.write_file(str(path))
        print("  wrote sophisticatedbackpacks.dat")
    elif stats['items_converted']:
        print("  WOULD write sophisticatedbackpacks.dat")


def _bp_has_contents(entry):
    items = _path(entry, 'contents', 'inventory', 'Items')
    return items is not None and len(items.tags) > 0


def _bp_fresh_uuid():
    return [random.randint(-2147483648, 2147483647) for _ in range(4)]


def _bp_rehome(idx, bc, S, O, stats):
    """Copy contents from the orphaned O-entry into the item's own S-entry in
    sophisticatedbackpacks.dat. Creates the S-entry if missing. Idempotent."""
    o_entry = idx.get(tuple(O))
    if o_entry is None or not _bp_has_contents(o_entry):
        return False
    s_entry = idx.get(tuple(S))
    if s_entry is not None and _bp_has_contents(s_entry):
        return False  # already rehomed
    src_contents = cget(o_entry, 'contents')
    if s_entry is None:
        s_entry = make_compound()
        cset(s_entry, 'uuid', make_ia(list(S)))
        cset(s_entry, 'contents', clone(src_contents, 'contents'))
        bc.tags.append(s_entry)
        idx[tuple(S)] = s_entry
    else:
        cset(s_entry, 'contents', clone(src_contents, 'contents'))
    stats['rehomed'] += 1
    return True


def _bp_handle_present(item, idx, bc, claimed, stats):
    """Repair a backpack item in place.

    Two states occur in a force-upgraded world:
      A) SB assigned a fresh storage_uuid (S) during migration and left the old
         contentsUuid (O) in custom_data -> rehome .dat[O] -> .dat[S].
      B) the item has NO storage_uuid, only custom_data (contentsUuid + slots)
         -> promote it: storage_uuid := contentsUuid, whose .dat entry still
         holds the (converted) contents, so no rehome is needed.
    Either way the custom_data residue is cleaned afterwards."""
    comps = cget(item, 'components')
    if comps is None:
        return False
    promoted = False
    if cget(comps, 'sophisticatedcore:storage_uuid') is None:
        # State B: lift custom_data up to real components first.
        promoted = promote_custom_data(item)
    S = cget(comps, 'sophisticatedcore:storage_uuid')
    if S is None:
        return promoted
    claimed.add(tuple(S.value))
    cd = cget(comps, 'minecraft:custom_data')
    O = cget(cd, 'contentsUuid') if cd is not None else None
    changed = False
    if O is not None and tuple(O.value) != tuple(S.value):
        if _bp_rehome(idx, bc, S.value, O.value, stats):
            changed = True
    if cd is not None:
        removed = False
        for k in ('contentsUuid', 'inventorySlots', 'upgradeSlots', 'renderInfo'):
            if cget(cd, k) is not None:
                cdel(cd, k)
                removed = True
        if len(cd.tags) == 0:
            cdel(comps, 'minecraft:custom_data')
            removed = True
        if removed:
            changed = True
    return changed or promoted


def _bp_pick_empty(idx, claimed):
    for u, e in idx.items():
        if u not in claimed and not _bp_has_contents(e):
            return list(u)
    return None


# Mapping from playerdata Dimension strings to world sub-directory prefixes.
_MC_DIM_TO_SUBDIR = {
    'minecraft:overworld': '',
    'minecraft:the_nether': 'DIM-1',
    'minecraft:the_end': 'DIM1',
}


def make_item_entity(item_1_21, pos_xyz):
    """Build a minecraft:item entity compound for a dropped item at pos_xyz.
    Age=-32768 prevents despawn so the player is guaranteed to find it."""
    uid = list(struct.unpack('>4i', uuid.uuid4().bytes))
    e = make_compound()
    cset(e, 'id', make_string('minecraft:item'))
    cset(e, 'UUID', make_ia(uid))
    pos = make_list(TAG_Double, name='Pos')
    for v in pos_xyz:
        pos.tags.append(make_double(v))
    cset(e, 'Pos', pos)
    motion = make_list(TAG_Double, name='Motion')
    for _ in range(3):
        motion.tags.append(make_double(0.0))
    cset(e, 'Motion', motion)
    rot = make_list(TAG_Float, name='Rotation')
    rot.tags.append(make_float(0.0))
    rot.tags.append(make_float(0.0))
    cset(e, 'Rotation', rot)
    cset(e, 'Age', make_short(-32768))
    cset(e, 'PickupDelay', make_short(0))
    item_copy = clone(item_1_21, name='Item')
    cdel(item_copy, 'Slot')
    cset(e, 'Item', item_copy)
    return e


def _spawn_item_in_entities(target_world, dim_str, pos_xyz, item_1_21, dry_run, stats):
    """Append a minecraft:item entity to the entities region chunk at pos_xyz.
    Returns True if the chunk was found and (unless dry_run) written."""
    dim_subdir = _MC_DIM_TO_SUBDIR.get(dim_str, '')
    if dim_str not in _MC_DIM_TO_SUBDIR:
        print(f"  !! unknown dimension '{dim_str}', defaulting to overworld for backpack spawn")

    px, py, pz = pos_xyz
    cx = int(px // 16)
    cz = int(pz // 16)
    rx = cx // 32
    rz = cz // 32
    lcx = cx % 32
    lcz = cz % 32

    ents_dir = _dim_base(target_world, dim_subdir) / 'entities'
    rpath = ents_dir / f'r.{rx}.{rz}.mca'
    if not rpath.exists():
        print(f"  !! {rpath} not found — worn backpack spawn failed (free a slot and re-run)")
        stats['worn_backpack_spawn_failed'] += 1
        return False

    try:
        rf = region.RegionFile(str(rpath))
        chunk = rf.get_chunk(lcx, lcz)
    except Exception as e:
        print(f"  !! chunk ({lcx},{lcz}) in {rpath.name}: {e} — worn backpack spawn failed")
        stats['worn_backpack_spawn_failed'] += 1
        return False

    if chunk is None:
        print(f"  !! chunk ({lcx},{lcz}) absent in {rpath.name} — worn backpack spawn failed")
        stats['worn_backpack_spawn_failed'] += 1
        return False

    ents = cget(chunk, 'Entities')
    if ents is None:
        ents = make_list(TAG_Compound, name='Entities')
        cset(chunk, 'Entities', ents)

    entity = make_item_entity(item_1_21, [px, py + 0.5, pz])
    ents.tags.append(entity)
    print(f"  spawning worn backpack at ({px:.1f}, {py + 0.5:.1f}, {pz:.1f}) in {rpath.name}")
    if not dry_run:
        rf.write_chunk(lcx, lcz, chunk)
    stats['worn_backpack_spawned'] += 1
    return True


def _free_inv_slots(n):
    """Main-inventory slots (0-35) not currently occupied in this player .dat.
    Armor (100-103) and offhand (-106) are excluded by construction."""
    inv = cget(n, 'Inventory')
    used = set()
    if inv is not None:
        for it in inv.tags:
            s = cget(it, 'Slot')
            if s is not None:
                used.add(int(s.value))
    return [s for s in range(36) if s not in used]


def _inv_has_backpack_uuid(n, uuid_val):
    """True if the player's main Inventory already holds a backpack whose
    storage_uuid matches uuid_val. Idempotency guard for worn relocation."""
    inv = cget(n, 'Inventory')
    if inv is None:
        return False
    want = list(uuid_val)
    for it in inv.tags:
        iid = cget(it, 'id')
        if iid is None or not is_backpack(iid.value):
            continue
        comps = cget(it, 'components')
        su = cget(comps, 'sophisticatedcore:storage_uuid') if comps is not None else None
        if su is not None and list(su.value) == want:
            return True
    return False


def _bp_relocate_worn(n, src_item, stats, overflow=None):
    """Place a worn-backpack item into a free main-inventory slot of player n
    instead of re-equipping it. The Accessories/SB mod drops a re-equipped
    backpack on load, but an ordinary inventory item survives and keeps its
    contents: the converted item carries sophisticatedcore:storage_uuid =
    the original contentsUuid, and .dat[contentsUuid] (converted by
    fix_backpack_dat) still holds the contents -- so no rehome is needed.

    If the inventory is full and overflow is provided, appends
    (item, pos_xyz, dim_str) to overflow so the caller can spawn the backpack
    as a ground entity at the player's location instead of losing it.

    Idempotent: skips if a backpack with the same storage_uuid is already in the
    inventory. Returns True only when it actually appends the item."""
    conv = item_no_slot(src_item)  # storage_uuid component == contentsUuid (O)
    comps = cget(conv, 'components')
    su = cget(comps, 'sophisticatedcore:storage_uuid') if comps is not None else None
    if su is None:
        return False  # not a real backpack / no storage id -> nothing to relocate
    if _inv_has_backpack_uuid(n, su.value):
        return False  # already relocated on a prior run
    inv = cget(n, 'Inventory')
    if inv is None:
        return False
    free = _free_inv_slots(n)
    if not free:
        stats['worn_backpack_no_free_slot'] += 1
        if overflow is not None:
            pos_tag = cget(n, 'Pos')
            dim_tag = cget(n, 'Dimension')
            if pos_tag is not None:
                pos_xyz = [t.value for t in pos_tag.tags]
                dim_str = dim_tag.value if dim_tag is not None else 'minecraft:overworld'
                overflow.append((conv, pos_xyz, dim_str))
        return False
    cset(conv, 'Slot', make_byte(free[0]))
    inv.tags.append(conv)
    stats['worn_backpack_relocated'] += 1
    return True


def _bp_collect_present(item, pairs, claimed, stats):
    """Region-only: record this backpack's (storage_uuid S, contentsUuid O) for
    the global .dat rehome, claim S, and clean the custom_data residue. Returns
    True if the item was modified (so the region needs rewriting)."""
    comps = cget(item, 'components')
    if comps is None:
        return False
    promoted = False
    if cget(comps, 'sophisticatedcore:storage_uuid') is None:
        # State B (no storage_uuid, only custom_data): promote so storage_uuid
        # := contentsUuid, whose .dat entry already holds the contents.
        promoted = promote_custom_data(item)
    S = cget(comps, 'sophisticatedcore:storage_uuid')
    if S is None:
        return promoted
    claimed.append(list(S.value))
    cd = cget(comps, 'minecraft:custom_data')
    O = cget(cd, 'contentsUuid') if cd is not None else None
    if O is not None and tuple(O.value) != tuple(S.value):
        pairs.append((list(S.value), list(O.value)))
        stats['region_backpack_pairs'] += 1
    changed = False
    if cd is not None:
        removed = False
        for k in ('contentsUuid', 'inventorySlots', 'upgradeSlots', 'renderInfo'):
            if cget(cd, k) is not None:
                cdel(cd, k)
                removed = True
        if len(cd.tags) == 0:
            cdel(comps, 'minecraft:custom_data')
            removed = True
        if removed:
            changed = True
    return changed or promoted


def make_backpack_collect_visitor(stats, pairs, claimed):
    """Region visitor (parallel path): collect backpack (S,O) pairs and claimed
    UUIDs; the actual .dat rehome happens once in migrate_global."""
    def visit(chunk):
        bes = cget(chunk, 'block_entities')
        if bes is None:
            return False
        changed = [False]

        def on_item(it):
            idt = cget(it, 'id')
            if idt is not None and isinstance(idt.value, str) and is_backpack(idt.value):
                if _bp_collect_present(it, pairs, claimed, stats):
                    changed[0] = True

        walk_items(bes, on_item)
        return changed[0]

    return visit


def fix_backpacks(target, sources, dry_run):
    """Restore backpack contents by rehoming them to each item's SB-assigned
    storage UUID in sophisticatedbackpacks.dat (items keep their UUID; SB won't
    accept a swapped-in stale one), and re-equip the worn backpack."""
    print("--- fix_backpacks ---")
    datpath = Path(target) / 'data' / 'sophisticatedbackpacks.dat'
    if not datpath.exists():
        print("  (no sophisticatedbackpacks.dat)")
        return
    dat = nbt_mod.NBTFile(str(datpath))
    bc = _path(dat, 'data', 'backpackContents')
    if bc is None:
        print("  (no backpackContents)")
        return
    idx = {tuple(cget(e, 'uuid').value): e for e in bc.tags if cget(e, 'uuid') is not None}
    stats = Counter()
    claimed = set()
    dat_changed = [False]

    def present(item):
        if is_backpack(cget(item, 'id').value):
            if _bp_handle_present(item, idx, bc, claimed, stats):
                dat_changed[0] = True
                return True
        return False

    # Region container backpacks (chests etc.)
    def visit(chunk):
        bes = cget(chunk, 'block_entities')
        ch = [False]
        if bes is not None:
            def on_item(it):
                if present(it):
                    ch[0] = True
            walk_items(bes, on_item)
        return ch[0]
    walk_region(target, 'region', visit, dry_run)

    # Playerdata: present items + worn restore
    pd = Path(target) / 'playerdata'
    if pd.exists():
        for f in sorted(pd.glob('*.dat')):
            try:
                n = nbt_mod.NBTFile(str(f))
            except Exception as e:
                print(f"  !! open {f.name}: {e}")
                continue
            fc = [False]

            def on_item(it):
                if present(it):
                    fc[0] = True
            walk_items(n, on_item)

            # Worn restore from source curios
            src_nbt = sources.playerdata.get(f.name)
            if src_nbt is not None:
                if _bp_restore_worn(n, src_nbt, idx, bc, claimed, stats):
                    fc[0] = True
                    dat_changed[0] = True

            if fc[0]:
                print(f"  {f.name}: {'rewriting' if not dry_run else 'WOULD rewrite'}")
                if not dry_run:
                    n.write_file(str(f))

    if dat_changed[0]:
        print(f"  sophisticatedbackpacks.dat: {'rewriting' if not dry_run else 'WOULD rewrite'}")
        if not dry_run:
            dat.write_file(str(datpath))
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


def _bp_restore_worn(n, src_nbt, idx, bc, claimed, stats, overflow=None):
    """Recover worn items the Curios -> NeoForge-Accessories migration dropped.

    Worn BACKPACKS are relocated into a free main-inventory slot (see
    _bp_relocate_worn) rather than re-equipped, because the accessories mod
    rejects a re-equipped backpack on load. Any OTHER worn curio is restored
    into its matching target curios attachment slot, as before.

    overflow, if provided, receives (item, pos_xyz, dim_str) tuples for any
    backpack that could not be placed due to a full inventory."""
    fc = cget(src_nbt, 'ForgeCaps')
    src_ci = cget(fc, 'curios:inventory') if fc is not None else None
    src_curios = cget(src_ci, 'Curios') if src_ci is not None else None
    if src_curios is None:
        return False
    att = cget(n, 'neoforge:attachments')
    tgt_ci = cget(att, 'curios:inventory') if att is not None else None
    tgt_curios = cget(tgt_ci, 'Curios') if tgt_ci is not None else None
    tgt_by_id = {cget(c, 'Identifier').value: c for c in tgt_curios.tags
                 if cget(c, 'Identifier') is not None} if tgt_curios is not None else {}
    changed = False
    for sc in src_curios.tags:
        ident = cget(sc, 'Identifier')
        s_items = _path(sc, 'StacksHandler', 'Stacks', 'Items')
        if s_items is None or len(s_items.tags) == 0:
            continue

        # Backpacks -> free inventory slot; everything else -> curios slot.
        rest = []
        for it in s_items.tags:
            iid = cget(it, 'id')
            if iid is not None and is_backpack(iid.value):
                if _bp_relocate_worn(n, it, stats, overflow=overflow):
                    changed = True
            else:
                rest.append(it)
        if not rest:
            continue

        # Restore non-backpack worn curios into the target attachment slot.
        tc = tgt_by_id.get(ident.value) if ident is not None else None
        if tc is None:
            continue
        t_stacks = _path(tc, 'StacksHandler', 'Stacks')
        if t_stacks is None:
            continue
        t_items = cget(t_stacks, 'Items')
        if t_items is not None and len(t_items.tags) > 0:
            continue  # already restored
        nl = make_list(TAG_Compound, name='Items')
        for it in rest:
            nl.tags.append(convert_item_1_20_to_1_21(it))
        cset(t_stacks, 'Items', nl)
        stats['worn_restored'] += 1
        changed = True
    return changed


def fix_worn_curios(target, sources, dry_run):
    """Restore worn accessories (e.g. the back-slot backpack) that the Forge
    Curios -> NeoForge Curios attachment migration dropped. Source: orig
    ForgeCaps.curios:inventory; target: neoforge:attachments.curios:inventory."""
    print("--- fix_worn_curios ---")
    pd = Path(target) / 'playerdata'
    if not pd.exists():
        print("  (no playerdata/)")
        return
    stats = Counter()
    for f in sorted(pd.glob('*.dat')):
        src_nbt = sources.playerdata.get(f.name)
        if src_nbt is None:
            continue
        fc = cget(src_nbt, 'ForgeCaps')
        src_ci = cget(fc, 'curios:inventory') if fc is not None else None
        if src_ci is None:
            continue
        src_curios = cget(src_ci, 'Curios')
        if src_curios is None:
            continue
        try:
            n = nbt_mod.NBTFile(str(f))
        except Exception as e:
            print(f"  !! open {f.name}: {e}")
            continue
        att = cget(n, 'neoforge:attachments')
        tgt_ci = cget(att, 'curios:inventory') if att is not None else None
        if tgt_ci is None:
            stats['no_target_curios'] += 1
            continue
        tgt_curios = cget(tgt_ci, 'Curios')
        if tgt_curios is None:
            stats['no_target_curios'] += 1
            continue
        tgt_by_id = {cget(c, 'Identifier').value: c for c in tgt_curios.tags
                     if cget(c, 'Identifier') is not None}
        changed = False
        for sc in src_curios.tags:
            ident = cget(sc, 'Identifier')
            if ident is None:
                continue
            sh = cget(sc, 'StacksHandler')
            stacks = cget(sh, 'Stacks') if sh is not None else None
            s_items = cget(stacks, 'Items') if stacks is not None else None
            if s_items is None or len(s_items.tags) == 0:
                continue
            tc = tgt_by_id.get(ident.value)
            if tc is None:
                stats['no_target_slot'] += 1
                continue
            t_stacks = cget(cget(tc, 'StacksHandler'), 'Stacks')
            if t_stacks is None:
                continue
            t_items = cget(t_stacks, 'Items')
            if t_items is not None and len(t_items.tags) > 0:
                stats['slot_already_filled'] += 1
                continue
            nl = make_list(TAG_Compound, name='Items')
            for it in s_items.tags:
                nl.tags.append(convert_item_1_20_to_1_21(it))
            cset(t_stacks, 'Items', nl)
            stats['restored_' + ident.value] += 1
            changed = True
        if changed:
            print(f"  {f.name}: {'rewriting' if not dry_run else 'WOULD rewrite'}")
            if not dry_run:
                n.write_file(str(f))
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


def fix_inv_backpacks(target, sources, dry_run):
    print("--- fix_inv_backpacks ---")
    stats = Counter()
    pd = Path(target) / 'playerdata'
    if not pd.exists():
        print("  (no playerdata/)")
        return

    for f in sorted(pd.glob('*.dat')):
        try:
            n = nbt_mod.NBTFile(str(f))
        except Exception as e:
            print(f"  !! open {f.name}: {e}")
            continue

        file_changed = [False]

        def on_item(item):
            tid = cget(item, 'id')
            if tid is None or not is_backpack(tid.value):
                return
            stats['backpacks_found'] += 1
            if promote_custom_data(item):
                stats['fixed'] += 1
                file_changed[0] = True

        walk_items(n, on_item)

        if file_changed[0]:
            print(f"  {f.name}: {'rewriting' if not dry_run else 'WOULD rewrite'}")
            if not dry_run:
                n.write_file(str(f))

    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


# ============================================================================
# Central item-stack converter: 1.20 {id,Count,tag} -> 1.21 {id,count,components}
# ============================================================================
# tag keys we know how to turn into proper 1.21 components. Anything not listed
# here is funnelled into minecraft:custom_data (exactly what vanilla DFU does),
# and logged so the real-world run surfaces what still needs a dedicated mapping.
UNMAPPED_TAG_KEYS = Counter()


def clone(tag, name=None):
    """Structurally deep-copy an NBT tag (optionally rename).

    copy.deepcopy fails on these tags (they hold an unpicklable _struct.Struct),
    so rebuild the tree by type.
    """
    nm = name if name is not None else tag.name
    if isinstance(tag, TAG_Compound):
        c = TAG_Compound()
        c.name = nm
        c.tags = [clone(x) for x in tag.tags]
        return c
    if isinstance(tag, TAG_List):
        c = TAG_List(type=TAGLIST[tag.tagID], name=nm)
        c.tags = [clone(x) for x in tag.tags]
        return c
    if isinstance(tag, (TAG_Int_Array, TAG_Byte_Array, TAG_Long_Array)):
        c = type(tag)(name=nm)
        c.value = list(tag.value)
        return c
    c = type(tag)(name=nm)
    c.value = tag.value
    return c


def _enchant_levels(ench_list, name):
    """[{id,lvl}, ...] -> compound {<id>: <lvl>}."""
    levels = make_compound(name='levels')
    for e in ench_list.tags:
        eid = cget(e, 'id')
        lvl = cget(e, 'lvl')
        if eid is not None and lvl is not None:
            cset(levels, str(eid.value), make_int(lvl.value))
    comp = make_compound(name=name)
    cset(comp, 'levels', levels)
    return comp


def _text_component_to_raw(s):
    """Canonicalize a 1.20 JSON text component to the 1.21 'raw' string form.
    A bare {"text":"X"} simplifies to the JSON string "X" (matching how vanilla
    re-serializes it); anything richer is kept verbatim (still valid JSON)."""
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and set(obj.keys()) == {'text'} and isinstance(obj['text'], str):
            return json.dumps(obj['text'], ensure_ascii=False)
    except Exception:
        pass
    return s


def _build_book_component(tag):
    """1.20 book tag (pages/title/author) -> minecraft:written_book_content or
    minecraft:writable_book_content. Returns (component_name, compound)."""
    pages = cget(tag, 'pages')
    title = cget(tag, 'title')
    author = cget(tag, 'author')
    if title is not None or author is not None:
        bc = make_compound(name='minecraft:written_book_content')
        pl = make_list(TAG_Compound, name='pages')
        if pages is not None:
            for p in pages.tags:
                e = make_compound()
                cset(e, 'raw', make_string(_text_component_to_raw(p.value)))
                pl.tags.append(e)
        cset(bc, 'pages', pl)
        if title is not None:
            tt = make_compound(name='title')
            cset(tt, 'raw', make_string(title.value))
            cset(bc, 'title', tt)
        if author is not None:
            cset(bc, 'author', make_string(author.value))
        gen = cget(tag, 'generation')
        if gen is not None:
            cset(bc, 'generation', make_int(gen.value))
        cset(bc, 'resolved', make_byte(1))
        return 'minecraft:written_book_content', bc
    # unsigned book & quill: pages are plain strings
    wc = make_compound(name='minecraft:writable_book_content')
    pl = make_list(TAG_Compound, name='pages')
    if pages is not None:
        for p in pages.tags:
            e = make_compound()
            cset(e, 'raw', make_string(p.value))
            pl.tags.append(e)
    cset(wc, 'pages', pl)
    return 'minecraft:writable_book_content', wc


_BANNER_COLOR = {0: 'white', 1: 'orange', 2: 'magenta', 3: 'light_blue', 4: 'yellow',
                 5: 'lime', 6: 'pink', 7: 'gray', 8: 'light_gray', 9: 'cyan', 10: 'purple',
                 11: 'blue', 12: 'brown', 13: 'green', 14: 'red', 15: 'black'}

# 1.20 banner pattern short code -> 1.21 namespaced pattern id (note the
# deliberately counterintuitive diagonals: rud=diagonal_right, rd=diagonal_up_right).
_BANNER_PATTERN = {
    'b': 'base', 'bs': 'stripe_bottom', 'ts': 'stripe_top', 'ls': 'stripe_left',
    'rs': 'stripe_right', 'cs': 'stripe_center', 'ms': 'stripe_middle',
    'drs': 'stripe_downright', 'dls': 'stripe_downleft', 'ss': 'small_stripes',
    'cr': 'cross', 'sc': 'straight_cross', 'ld': 'diagonal_left', 'rud': 'diagonal_right',
    'lud': 'diagonal_up_left', 'rd': 'diagonal_up_right', 'vh': 'half_vertical',
    'vhr': 'half_vertical_right', 'hh': 'half_horizontal', 'hhb': 'half_horizontal_bottom',
    'bl': 'square_bottom_left', 'br': 'square_bottom_right', 'tl': 'square_top_left',
    'tr': 'square_top_right', 'bt': 'triangle_bottom', 'tt': 'triangle_top',
    'bts': 'triangles_bottom', 'tts': 'triangles_top', 'mc': 'circle', 'mr': 'rhombus',
    'bo': 'border', 'cbo': 'curly_border', 'bri': 'bricks', 'gra': 'gradient',
    'gru': 'gradient_up', 'cre': 'creeper', 'sku': 'skull', 'flo': 'flower',
    'moj': 'mojang', 'glb': 'globe', 'pig': 'piglin', 'flw': 'flow', 'gus': 'guster',
}


def _banner_pattern_id(code):
    if code in _BANNER_PATTERN:
        return 'minecraft:' + _BANNER_PATTERN[code]
    if ':' in code:
        return code  # already namespaced (modded / pre-converted)
    UNMAPPED_TAG_KEYS['banner_pattern:' + code] += 1
    return 'minecraft:' + code


def build_components(tag):
    """Build a 1.21 components compound from a 1.20 item 'tag' compound.

    Well-understood, schema-stable keys are mapped to real components; everything
    else is preserved under minecraft:custom_data and logged.
    """
    comp = make_compound(name='components')
    leftover = make_compound(name='minecraft:custom_data')
    consumed = set()

    # --- Books (written / writable) ---
    if cget(tag, 'pages') is not None:
        name, bookcomp = _build_book_component(tag)
        cset(comp, name, bookcomp)
        consumed |= {'pages', 'title', 'author', 'resolved', 'generation',
                     'filtered_pages', 'filtered_title'}

    # --- BlockEntityTag: containers (shulkers) and banners/shields ---
    bet = cget(tag, 'BlockEntityTag')
    if bet is not None:
        items = cget(bet, 'Items')
        patterns = cget(bet, 'Patterns')
        if items is not None and len(items.tags) > 0:
            cl = make_list(TAG_Compound, name='minecraft:container')
            for it in items.tags:
                slot = cget(it, 'Slot')
                e = make_compound()
                cset(e, 'slot', make_int(slot.value if slot is not None else 0))
                cset(e, 'item', item_no_slot(it))
                cl.tags.append(e)
            cset(comp, 'minecraft:container', cl)
            others = [x for x in bet.tags if x.name not in ('id', 'ForgeCaps', 'Items')]
            if others:
                bed = make_compound(name='minecraft:block_entity_data')
                bid = cget(bet, 'id')
                if bid is not None:
                    cset(bed, 'id', make_string(bid.value))
                for x in others:
                    bed.tags.append(clone(x))
                cset(comp, 'minecraft:block_entity_data', bed)
            consumed.add('BlockEntityTag')
        elif patterns is not None and len(patterns.tags) > 0:
            # Banner / shield: {Pattern:<code>, Color:<0-15>} -> banner_patterns
            bp = make_list(TAG_Compound, name='minecraft:banner_patterns')
            for p in patterns.tags:
                code = cget(p, 'Pattern')
                col = cget(p, 'Color')
                e = make_compound()
                cset(e, 'pattern', make_string(_banner_pattern_id(code.value if code else 'b')))
                cset(e, 'color', make_string(_BANNER_COLOR.get(col.value if col is not None else 0, 'white')))
                bp.tags.append(e)
            cset(comp, 'minecraft:banner_patterns', bp)
            base = cget(bet, 'Base')  # shields carry a base color here
            if base is not None:
                cset(comp, 'minecraft:base_color', make_string(_BANNER_COLOR.get(base.value, 'white')))
            consumed.add('BlockEntityTag')

    for t in tag.tags:
        k = t.name
        if k in consumed:
            continue
        try:
            if k == 'Damage':
                if int(t.value) != 0:  # 0 is default; vanilla omits it
                    cset(comp, 'minecraft:damage', make_int(t.value))
            elif k == 'RepairCost':
                cset(comp, 'minecraft:repair_cost', make_int(t.value))
            elif k == 'Unbreakable':
                cset(comp, 'minecraft:unbreakable', make_compound())
            elif k == 'CustomModelData':
                cset(comp, 'minecraft:custom_model_data', make_int(t.value))
            elif k == 'Enchantments':
                cset(comp, 'minecraft:enchantments', _enchant_levels(t, 'minecraft:enchantments'))
            elif k == 'StoredEnchantments':
                cset(comp, 'minecraft:stored_enchantments', _enchant_levels(t, 'minecraft:stored_enchantments'))
            elif k == 'display':
                nm = cget(t, 'Name')
                if nm is not None:
                    cset(comp, 'minecraft:custom_name', make_string(_text_component_to_raw(nm.value)))
                lore = cget(t, 'Lore')
                if lore is not None:
                    nl = make_list(TAG_String, name='minecraft:lore')
                    for ln in lore.tags:
                        nl.tags.append(make_string(_text_component_to_raw(ln.value)))
                    cset(comp, 'minecraft:lore', nl)
                col = cget(t, 'color')
                if col is not None:
                    dc = make_compound(name='minecraft:dyed_color')
                    cset(dc, 'rgb', make_int(col.value))
                    cset(comp, 'minecraft:dyed_color', dc)
                for d in t.tags:
                    if d.name not in ('Name', 'Lore', 'color'):
                        leftover.tags.append(clone(d))
                        UNMAPPED_TAG_KEYS['display.' + d.name] += 1
            elif k == 'BlockEntityTag':
                cset(comp, 'minecraft:block_entity_data', clone(t, 'minecraft:block_entity_data'))
            elif k == 'BlockStateTag':
                cset(comp, 'minecraft:block_state', clone(t, 'minecraft:block_state'))
            elif k == 'Trim':
                cset(comp, 'minecraft:trim', clone(t, 'minecraft:trim'))
            elif k == 'Potion':
                pc = make_compound(name='minecraft:potion_contents')
                cset(pc, 'potion', make_string(t.value))
                cset(comp, 'minecraft:potion_contents', pc)
            else:
                # Unknown / mod-namespaced / hard ones (HideFlags, ...)
                leftover.tags.append(clone(t))
                UNMAPPED_TAG_KEYS[k] += 1
        except Exception:
            leftover.tags.append(clone(t))
            UNMAPPED_TAG_KEYS[k + ' (ERROR)'] += 1

    if leftover.tags:
        cset(comp, 'minecraft:custom_data', leftover)
    return comp


def convert_item_1_20_to_1_21(src):
    """Return a fresh 1.21 item compound from a 1.20 (or already-1.21) item.

    Preserves a leading 'Slot' field if present (Create inventories store Slot
    inside the stack compound).
    """
    new = make_compound()
    # Slot (kept as-is; may be Byte or Int)
    slot = cget(src, 'Slot')
    if slot is not None:
        new.tags.append(clone(slot, 'Slot'))
    idt = cget(src, 'id')
    if idt is not None:
        cset(new, 'id', make_string(idt.value))
    cnt = cget(src, 'Count')
    if cnt is None:
        cnt = cget(src, 'count')
    cset(new, 'count', make_int(cnt.value if cnt is not None else 1))
    idval = idt.value if idt is not None else ''
    tag = cget(src, 'tag')
    if tag is not None and len(tag.tags) > 0:
        comp = build_mod_components(idval, tag)
        if len(comp.tags) > 0:
            cset(new, 'components', comp)
    else:
        # already-1.21 style components passthrough
        existing = cget(src, 'components')
        if existing is not None and len(existing.tags) > 0:
            cset(new, 'components', clone(existing, 'components'))
    return new


def item_no_slot(src):
    """Convert a 1.20 stack and drop the leading Slot (used where slot is a sibling)."""
    it = convert_item_1_20_to_1_21(src)
    cdel(it, 'Slot')
    return it


# ============================================================================
# Mod-specific legacy-tag -> 1.21 components builders. These accept either a
# 1.20 item 'tag' compound OR a broken-world 'minecraft:custom_data' compound
# (vanilla DFU dumps the whole old tag there for items it doesn't recognise),
# since both carry the same legacy keys.
# ============================================================================
def comp_create_filter(tag):
    comp = make_compound(name='components')
    fl = make_list(TAG_Compound, name='create:filter_items')
    items_outer = cget(tag, 'Items')
    inner = cget(items_outer, 'Items') if items_outer is not None else None
    if inner is not None:
        for it in inner.tags:
            slot = cget(it, 'Slot')
            entry = make_compound()
            cset(entry, 'item', item_no_slot(it))
            cset(entry, 'slot', make_int(slot.value if slot is not None else 0))
            fl.tags.append(entry)
    cset(comp, 'create:filter_items', fl)
    rn = cget(tag, 'RespectNBT')
    cset(comp, 'create:filter_items_respect_nbt', make_byte(rn.value if rn is not None else 0))
    bl = cget(tag, 'Blacklist')
    cset(comp, 'create:filter_items_blacklist', make_byte(bl.value if bl is not None else 0))
    return comp


_ATTR_WM = {0: 'whitelist_disj', 1: 'whitelist_conj', 2: 'blacklist'}


def comp_attribute_filter(tag):
    comp = make_compound(name='components')
    wm = cget(tag, 'WhitelistMode')
    cset(comp, 'create:attribute_filter_whitelist_mode',
         make_string(_ATTR_WM.get(wm.value if wm is not None else 0, 'whitelist_disj')))
    ma = cget(tag, 'MatchedAttributes')
    nl = make_list(TAG_Compound, name='create:attribute_filter_matched_attributes')
    if ma is not None:
        for e in ma.tags:
            inv = cget(e, 'Inverted')
            aid = cget(e, 'attributeId')
            ne = make_compound()
            cset(ne, 'inverted', make_byte(inv.value if inv is not None else 0))
            attr = make_compound(name='attribute')
            cset(attr, 'type', make_string(aid.value if aid is not None else ''))
            cset(attr, 'value', make_compound())
            cset(ne, 'attribute', attr)
            nl.tags.append(ne)
    cset(comp, 'create:attribute_filter_matched_attributes', nl)
    return comp


def _ordered_stacks(src_os):
    """1.20 OrderedStacks{Entries[{Item,Amount}]} -> 1.21 {entries[{item_stack,count}]}."""
    ostacks = make_compound(name='ordered_stacks')
    entries = make_list(TAG_Compound, name='entries')
    src_entries = cget(src_os, 'Entries') if src_os is not None else None
    if src_entries is not None:
        for en in src_entries.tags:
            item = cget(en, 'Item')
            amt = cget(en, 'Amount')
            ne = make_compound()
            cset(ne, 'item_stack', item_no_slot(item) if item is not None else make_compound())
            cset(ne, 'count', make_int(amt.value if amt is not None else 1))
            entries.tags.append(ne)
    cset(ostacks, 'entries', entries)
    return ostacks


def comp_package(tag):
    comp = make_compound(name='components')
    # contents
    cl = make_list(TAG_Compound, name='create:package_contents')
    items_outer = cget(tag, 'Items')
    inner = cget(items_outer, 'Items') if items_outer is not None else None
    if inner is not None:
        for it in inner.tags:
            slot = cget(it, 'Slot')
            entry = make_compound()
            cset(entry, 'item', item_no_slot(it))
            cset(entry, 'slot', make_int(slot.value if slot is not None else 0))
            cl.tags.append(entry)
    cset(comp, 'create:package_contents', cl)
    # order data
    frag = cget(tag, 'Fragment')
    if frag is not None:
        od = make_compound(name='create:package_order_data')
        for sk, dk, mk in (('IsFinalLink', 'is_final_link', make_byte),
                           ('IsFinal', 'is_final', make_byte),
                           ('LinkIndex', 'link_index', make_int),
                           ('Index', 'fragment_index', make_int),
                           ('OrderId', 'order_id', make_int)):
            v = cget(frag, sk)
            if v is not None:
                cset(od, dk, mk(v.value))
        octx = make_compound(name='order_context')
        cset(octx, 'ordered_crafts', make_list(TAG_Compound, name='ordered_crafts'))
        oc = cget(frag, 'OrderContext')
        cset(octx, 'ordered_stacks', _ordered_stacks(cget(oc, 'OrderedStacks') if oc is not None else None))
        cset(od, 'order_context', octx)
        cset(comp, 'create:package_order_data', od)
    addr = cget(tag, 'Address')
    cset(comp, 'create:package_address', make_string(addr.value if addr is not None else ''))
    return comp


def _extract_plain_text(s):
    """A 1.20 clipboard line is a JSON text component; 1.21 wants plain text."""
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and 'text' in obj:
            return obj['text']
        if isinstance(obj, str):
            return obj
    except Exception:
        pass
    return s


_CLIPBOARD_TYPE = {0: 'empty', 1: 'written', 2: 'editing'}


def build_clipboard_content(tag):
    """1.20 clipboard tag -> create:clipboard_content compound (used by BE and item)."""
    cc = make_compound(name='create:clipboard_content')
    ty = cget(tag, 'Type')
    cset(cc, 'type', make_string(_CLIPBOARD_TYPE.get(ty.value if ty is not None else 1, 'written')))
    pop = cget(tag, 'PreviouslyOpenedPage')
    cset(cc, 'previously_opened_page', make_int(pop.value if pop is not None else 0))
    cset(cc, 'read_only', make_byte(0))
    pages = make_list(TAG_List, name='pages')
    pages_src = cget(tag, 'Pages')
    if pages_src is not None:
        for pg in pages_src.tags:
            plist = make_list(TAG_Compound)
            entries = cget(pg, 'Entries')
            if entries is not None:
                for en in entries.tags:
                    ne = make_compound()
                    cset(ne, 'item_amount', make_int(0))
                    chk = cget(en, 'Checked')
                    cset(ne, 'checked', make_byte(chk.value if chk is not None else 0))
                    cset(ne, 'icon', make_compound())
                    txt = cget(en, 'Text')
                    cset(ne, 'text', make_string(_extract_plain_text(txt.value if txt is not None else '')))
                    plist.tags.append(ne)
            pages.tags.append(plist)
    cset(cc, 'pages', pages)
    return cc


def comp_clipboard(tag):
    comp = make_compound(name='components')
    cset(comp, 'create:clipboard_content', build_clipboard_content(tag))
    return comp


def comp_sophisticated_backpack(tag):
    """1.20 backpack tag {contentsUuid, inventorySlots, upgradeSlots, renderInfo}
    -> sophisticatedcore:* components."""
    comp = make_compound(name='components')
    uuid = cget(tag, 'contentsUuid')
    if uuid is not None:
        cset(comp, 'sophisticatedcore:storage_uuid', make_ia(uuid.value))
    inv = cget(tag, 'inventorySlots')
    if inv is not None:
        cset(comp, 'sophisticatedcore:number_of_inventory_slots', make_int(inv.value))
    up = cget(tag, 'upgradeSlots')
    if up is not None:
        cset(comp, 'sophisticatedcore:number_of_upgrade_slots', make_int(up.value))
    ri = cget(tag, 'renderInfo')
    if ri is not None:
        cset(comp, 'sophisticatedcore:render_info_tag', clone(ri, 'sophisticatedcore:render_info_tag'))
    # preserve anything else under custom_data
    leftover = make_compound(name='minecraft:custom_data')
    for t in tag.tags:
        if t.name not in ('contentsUuid', 'inventorySlots', 'upgradeSlots', 'renderInfo'):
            leftover.tags.append(clone(t))
    if leftover.tags:
        cset(comp, 'minecraft:custom_data', leftover)
    return comp


# id -> legacy-tag->components builder
MOD_ITEM_CONVERTERS = {
    'create:filter': comp_create_filter,
    'create:attribute_filter': comp_attribute_filter,
    'create:clipboard': comp_clipboard,
}


def mod_converter_for(idval):
    if idval in MOD_ITEM_CONVERTERS:
        return MOD_ITEM_CONVERTERS[idval]
    if idval.startswith('create:') and 'package' in idval:
        return comp_package
    if idval.startswith('sophisticatedbackpacks:') and 'backpack' in idval:
        return comp_sophisticated_backpack
    return None


def build_mod_components(idval, tag):
    """Dispatch to a mod-specific builder, else the generic vanilla mapping."""
    conv = mod_converter_for(idval)
    if conv is not None:
        return conv(tag)
    return build_components(tag)


# ============================================================================
# Generic mod-inventory restore: parallel-walk target & source block entities,
# rewriting every embedded item stack from the converted source stack.
# ============================================================================
def is_item_stack(c):
    if not isinstance(c, TAG_Compound):
        return False
    idt = cget(c, 'id')
    if idt is None or not isinstance(idt.value, str) or ':' not in idt.value:
        return False
    return has(c, 'Count') or has(c, 'count')


def _src_count(s):
    c = cget(s, 'Count')
    if c is None:
        c = cget(s, 'count')
    return int(c.value) if c is not None else 1


def _tgt_count(t):
    c = cget(t, 'count')
    if c is None:
        c = cget(t, 'Count')
    return int(c.value) if c is not None else None


def _apply_stack(tgt, src, stats):
    """Update the target stack's count/components from the converted source, but
    only when they actually differ — compare against the converted result so it
    is truly idempotent (items whose 1.20 tag maps to empty components, e.g. a
    tool with only Damage:0, no longer re-trigger forever). Preserves the
    target's Slot and any extra fields."""
    conv = convert_item_1_20_to_1_21(src)
    conv_count = cget(conv, 'count')
    conv_count_v = conv_count.value if conv_count is not None else 1
    conv_comp = cget(conv, 'components')
    if _tgt_count(tgt) == conv_count_v and nbt_equal(cget(tgt, 'components'), conv_comp):
        return False
    cset(tgt, 'count', make_int(conv_count_v))
    cdel(tgt, 'Count')
    if conv_comp is not None:
        cset(tgt, 'components', conv_comp)
    else:
        cdel(tgt, 'components')
    cid = cget(conv, 'id')
    if cid is not None:
        cset(tgt, 'id', cid)
    stats['stacks_restored'] += 1
    return True


def restore_stacks_parallel(tgt, src, stats):
    """Walk target & source structurally; restore item stacks from source.
    Returns True if anything changed under tgt."""
    changed = False
    if is_item_stack(tgt) and is_item_stack(src):
        return _apply_stack(tgt, src, stats)

    if isinstance(tgt, TAG_Compound) and isinstance(src, TAG_Compound):
        for t in tgt.tags:
            s = cget(src, t.name)
            if s is not None and type(s) is type(t):
                if restore_stacks_parallel(t, s, stats):
                    changed = True
    elif isinstance(tgt, TAG_List) and isinstance(src, TAG_List):
        tl, sl = tgt.tags, src.tags
        if tl and sl and is_item_stack(tl[0]) and is_item_stack(sl[0]):
            # match by Slot if both sides carry it, else by index
            both_slot = all(cget(e, 'Slot') is not None for e in tl) and \
                        all(cget(e, 'Slot') is not None for e in sl)
            if both_slot:
                by_slot = {cget(e, 'Slot').value: e for e in sl}
                for te in tl:
                    se = by_slot.get(cget(te, 'Slot').value)
                    if se is not None and _apply_stack(te, se, stats):
                        changed = True
            else:
                for te, se in zip(tl, sl):
                    if _apply_stack(te, se, stats):
                        changed = True
        else:
            for te, se in zip(tl, sl):
                if restore_stacks_parallel(te, se, stats):
                    changed = True
    return changed


def make_inventories_visitor(sources, stats):
    def visit(chunk):
        bes = cget(chunk, 'block_entities')
        if bes is None:
            return False
        changed = False
        for be in bes.tags:
            tid = cget(be, 'id')
            x, y, z = cget(be, 'x'), cget(be, 'y'), cget(be, 'z')
            if tid is None or x is None or y is None or z is None:
                continue
            src = sources.be(tid.value, x.value, y.value, z.value)
            if src is None:
                continue
            stats['be_matched'] += 1
            if restore_stacks_parallel(be, src, stats):
                stats['be_changed'] += 1
                changed = True
        return changed

    return visit


def fix_inventories(target, sources, dry_run):
    """Generic count/contents restore across all matched block entities."""
    print("--- fix_inventories (generic mod-inventory sweep) ---")
    stats = Counter()
    walk_region(target, 'region', make_inventories_visitor(sources, stats), dry_run)
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")
    if UNMAPPED_TAG_KEYS:
        print("  -- tag keys routed to custom_data --")
        for k, v in UNMAPPED_TAG_KEYS.most_common():
            print(f"     {k}: {v}")


# ============================================================================
# Structural fixers (wiped/reshaped payloads restored from 1.20 source)
# ============================================================================
def make_be_fixer_visitor(sources, stats, mc_id, per_be):
    def visit(chunk):
        bes = cget(chunk, 'block_entities')
        if bes is None:
            return False
        changed = False
        for be in bes.tags:
            tid = cget(be, 'id')
            if tid is None or tid.value != mc_id:
                continue
            stats['target_total'] += 1
            x, y, z = cget(be, 'x'), cget(be, 'y'), cget(be, 'z')
            if x is None or y is None or z is None:
                continue
            src = sources.be(mc_id, x.value, y.value, z.value)
            if src is None:
                stats['no_source_match'] += 1
                continue
            if per_be(be, src, stats):
                changed = True
        return changed

    return visit


def _be_fixer(target, sources, dry_run, mc_id, per_be):
    """Shared scaffold: walk region BEs of mc_id, match source by (x,y,z),
    call per_be(target_be, source_be, stats)->bool(changed)."""
    stats = Counter()
    walk_region(target, 'region', make_be_fixer_visitor(sources, stats, mc_id, per_be), dry_run)
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


def _per_be_fluid_tank(be, src, stats):
    src_tc = cget(src, 'TankContent')
    if src_tc is None:
        return False
    amt = cget(src_tc, 'Amount')
    fname = cget(src_tc, 'FluidName')
    if amt is None or fname is None or int(amt.value) <= 0:
        return False
    tgt_tc = cget(be, 'TankContent')
    if tgt_tc is None:
        tgt_tc = make_compound()
        cset(be, 'TankContent', tgt_tc)
    if cget(tgt_tc, 'Fluid') is not None:
        stats['already_fixed'] += 1
        return False
    fluid = make_compound(name='Fluid')
    cset(fluid, 'amount', make_int(amt.value))
    cset(fluid, 'id', make_string(fname.value))
    cset(tgt_tc, 'Fluid', fluid)
    stats['fixed'] += 1
    return True


def fix_fluid_tanks(target, sources, dry_run):
    print("--- fix_fluid_tanks ---")
    _be_fixer(target, sources, dry_run, 'create:fluid_tank', _per_be_fluid_tank)


_PANEL_SLOTS = ('top_left', 'top_right', 'bottom_left', 'bottom_right')
_PANEL_SLOT_NAME = {0: 'top_left', 1: 'top_right', 2: 'bottom_left', 3: 'bottom_right'}


def _xyzslot(e):
    return (cget(e, 'X').value, cget(e, 'Y').value, cget(e, 'Z').value,
            cget(e, 'Slot').value if cget(e, 'Slot') is not None else 0)


def _per_be_factory_panel(be, src, stats):
    changed = False
    for sn in _PANEL_SLOTS:
        s_slot = cget(src, sn)
        t_slot = cget(be, sn)
        if s_slot is None or t_slot is None:
            continue
        # Targeting: {X,Y,Z,Slot} -> {pos:[x,y,z], slot:<name>}
        s_t = cget(s_slot, 'Targeting')
        t_t = cget(t_slot, 'Targeting')
        if s_t is not None and len(s_t.tags) > 0 and (t_t is None or len(t_t.tags) == 0):
            nl = make_list(TAG_Compound, name='Targeting')
            for e in s_t.tags:
                x, y, z, sl = _xyzslot(e)
                ne = make_compound()
                cset(ne, 'pos', make_ia([x, y, z]))
                cset(ne, 'slot', make_string(_PANEL_SLOT_NAME.get(sl, 'bottom_left')))
                nl.tags.append(ne)
            cset(t_slot, 'Targeting', nl)
            stats['targeting_fixed'] += 1
            changed = True
        # TargetedBy: {X,Y,Z,Slot,Amount,ArrowBending} -> {amount,arrow_bending,position{pos,slot}}
        s_tb = cget(s_slot, 'TargetedBy')
        t_tb = cget(t_slot, 'TargetedBy')
        if s_tb is not None and len(s_tb.tags) > 0 and (t_tb is None or len(t_tb.tags) == 0):
            nl = make_list(TAG_Compound, name='TargetedBy')
            for e in s_tb.tags:
                x, y, z, sl = _xyzslot(e)
                amount = cget(e, 'Amount')
                bending = cget(e, 'ArrowBending')
                ne = make_compound()
                cset(ne, 'amount', make_int(amount.value if amount is not None else 0))
                cset(ne, 'arrow_bending', make_int(bending.value if bending is not None else -1))
                pos = make_compound(name='position')
                cset(pos, 'pos', make_ia([x, y, z]))
                cset(pos, 'slot', make_string(_PANEL_SLOT_NAME.get(sl, 'bottom_left')))
                cset(ne, 'position', pos)
                nl.tags.append(ne)
            cset(t_slot, 'TargetedBy', nl)
            stats['targetedby_fixed'] += 1
            changed = True
    return changed


def fix_factory_panels(target, sources, dry_run):
    print("--- fix_factory_panels ---")
    _be_fixer(target, sources, dry_run, 'create:factory_panel', _per_be_factory_panel)


def _per_be_table_cloth(be, src, stats):
    src_req = cget(src, 'EncodedRequest')
    src_valid = cget(src, 'Valid')
    # Only the shop cloths carry a real request / valid flag.
    if src_valid is None or int(src_valid.value) == 0:
        return False
    rd = cget(be, 'RequestData')
    if rd is not None:
        iv = cget(rd, 'is_valid')
        if iv is not None and int(iv.value) == 1:
            stats['already_fixed'] += 1
            return False
    rd = make_compound(name='RequestData')
    dim = cget(src, 'TargetDim')
    cset(rd, 'target_dim', make_string(dim.value if dim and dim.value else 'null'))
    off = cget(src, 'TargetOffset')
    if off is not None:
        cset(rd, 'target_offset', xyz_compound_to_ia(off))
    else:
        cset(rd, 'target_offset', make_ia([0, 0, 0]))
    enc = make_compound(name='encoded_request')
    cset(enc, 'ordered_crafts', make_list(TAG_Compound, name='ordered_crafts'))
    cset(enc, 'ordered_stacks',
         _ordered_stacks(cget(src_req, 'OrderedStacks') if src_req is not None else None))
    cset(rd, 'encoded_request', enc)
    addr = cget(src, 'EncodedAddress')
    cset(rd, 'encoded_target_address', make_string(addr.value if addr is not None else ''))
    cset(rd, 'is_valid', make_byte(1))
    cset(be, 'RequestData', rd)
    stats['fixed'] += 1
    return True


def fix_table_cloths(target, sources, dry_run):
    print("--- fix_table_cloths (shops) ---")
    _be_fixer(target, sources, dry_run, 'create:table_cloth', _per_be_table_cloth)


def _per_be_clipboard(be, src, stats):
    src_item = cget(src, 'Item')
    src_tag = cget(src_item, 'tag') if src_item is not None else None
    if src_tag is None:
        stats['no_source_tag'] += 1
        return False
    components = cget(be, 'components')
    if components is None:
        components = make_compound()
        cset(be, 'components', components)
    if cget(components, 'create:clipboard_content') is not None:
        stats['already_fixed'] += 1
        return False
    cset(components, 'create:clipboard_content', build_clipboard_content(src_tag))
    stats['fixed'] += 1
    return True


def fix_clipboards(target, sources, dry_run):
    print("--- fix_clipboards ---")
    _be_fixer(target, sources, dry_run, 'create:clipboard', _per_be_clipboard)


def make_package_entities_visitor(sources, stats):
    def visit(chunk):
        ents = cget(chunk, 'Entities')
        if ents is None:
            return False
        changed = False
        for ent in ents.tags:
            tid = cget(ent, 'id')
            if tid is None or tid.value != 'create:package':
                continue
            stats['target_total'] += 1
            u = cget(ent, 'UUID')
            src = sources.entity_by_uuid(u.value) if u is not None else None
            if src is None:
                stats['no_source_match'] += 1
                continue
            src_box = cget(src, 'Box')
            if src_box is None or cget(src_box, 'tag') is None:
                continue
            tgt_box = cget(ent, 'Box')
            comp = cget(tgt_box, 'components') if tgt_box is not None else None
            if comp is not None and cget(comp, 'create:package_contents') is not None:
                stats['already_fixed'] += 1
                continue
            cset(ent, 'Box', convert_item_1_20_to_1_21(src_box))
            stats['fixed'] += 1
            changed = True
        return changed

    return visit


def fix_package_entities(target, sources, dry_run):
    """Restore loose create:package entities whose Box item lost its contents."""
    print("--- fix_package_entities ---")
    stats = Counter()
    walk_region(target, 'entities', make_package_entities_visitor(sources, stats), dry_run)
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


_POSTBOX_TARGET_TYPE = {'TrainStation': 'create:train_station'}


def _per_be_postbox(be, src, stats):
    src_t = cget(src, 'Target')
    if src_t is None:
        return False
    tgt_t = cget(be, 'Target')
    if tgt_t is not None and cget(tgt_t, 'type') is not None:
        stats['already_fixed'] += 1
        return False
    typ = cget(src_t, 'Type')
    rel = cget(src_t, 'RelativePos')
    nt = make_compound(name='Target')
    if typ is not None:
        cset(nt, 'type', make_string(_POSTBOX_TARGET_TYPE.get(typ.value, typ.value)))
    if rel is not None:
        cset(nt, 'relative_pos', xyz_compound_to_ia(rel))
    cset(be, 'Target', nt)
    stats['fixed'] += 1
    return True


def fix_postboxes(target, sources, dry_run):
    """Restore the postbox Target (linked station) lost on migration."""
    print("--- fix_postboxes ---")
    _be_fixer(target, sources, dry_run, 'create:package_postbox', _per_be_postbox)


# ============================================================================
# Mod-items in vanilla containers (playerdata + region): vanilla DFU preserved
# the legacy tag under minecraft:custom_data. Rebuild proper components from it.
# ============================================================================
def reconstruct_from_custom_data(item, stats):
    """If a known mod-item has its legacy tag parked in custom_data, rebuild the
    proper components from it. Returns True if changed."""
    tid = cget(item, 'id')
    if tid is None or not isinstance(tid.value, str):
        return False
    if is_backpack(tid.value):
        return False  # backpacks are rehomed by fix_backpacks (storage is in the .dat)
    conv = mod_converter_for(tid.value)
    if conv is None:
        return False
    components = cget(item, 'components')
    if components is None:
        return False
    cd = cget(components, 'minecraft:custom_data')
    if cd is None or len(cd.tags) == 0:
        return False
    new_comp = conv(cd)
    if len(new_comp.tags) == 0:
        return False
    # Inventory/container packages tied to a (now stale) logistics order get
    # deleted by Create on load — drop the order data so it's a plain sealed
    # package that keeps its contents.
    if 'package' in tid.value:
        cdel(new_comp, 'create:package_order_data')
    for t in new_comp.tags:
        cset(components, t.name, t)
    cdel(components, 'minecraft:custom_data')
    stats['mod_items_rebuilt'] += 1
    return True


def make_mod_items_visitor(sources, stats):
    """Region visitor: rebuild mod-item components from custom_data in
    vanilla containers (chests/barrels carrying mod items). sources unused."""
    def visit(chunk):
        changed = [False]

        def on_item(item):
            if reconstruct_from_custom_data(item, stats):
                changed[0] = True

        bes = cget(chunk, 'block_entities')
        if bes is not None:
            walk_items(bes, on_item)
        return changed[0]

    return visit


def fix_mod_items_playerdata(target, dry_run, stats=None):
    """Global: rebuild mod-item components from custom_data in playerdata."""
    if stats is None:
        stats = Counter()
    pd = Path(target) / 'playerdata'
    if pd.exists():
        for f in sorted(pd.glob('*.dat')):
            try:
                n = nbt_mod.NBTFile(str(f))
            except Exception as e:
                print(f"  !! open {f.name}: {e}")
                continue
            file_changed = [False]

            def on_item(item):
                if reconstruct_from_custom_data(item, stats):
                    file_changed[0] = True

            walk_items(n, on_item)
            if file_changed[0]:
                print(f"  {f.name}: {'rewriting' if not dry_run else 'WOULD rewrite'}")
                if not dry_run:
                    n.write_file(str(f))
    return stats


def fix_mod_items(target, sources, dry_run):
    """Rebuild mod-item components from custom_data in region + playerdata."""
    print("--- fix_mod_items (custom_data -> components) ---")
    stats = Counter()
    walk_region(target, 'region', make_mod_items_visitor(sources, stats), dry_run)
    fix_mod_items_playerdata(target, dry_run, stats)
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


# ============================================================================
# Verify: compare patched workdir against the 1.21-target (positional twin)
# on the meaningful per-feature fields. Independently-generated identifiers
# (Freq/Owner/UUID/order_id/timers) and '!'-removed-component markers are
# ignored — structure and restored values are what matter.
# ============================================================================
def nbt_equal(a, b):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (TAG_Compound, NBTFile)) and isinstance(b, (TAG_Compound, NBTFile)):
        ka = {t.name for t in a.tags if not t.name.startswith('!')}
        kb = {t.name for t in b.tags if not t.name.startswith('!')}
        if ka != kb:
            return False
        return all(nbt_equal(cget(a, k), cget(b, k)) for k in ka)
    if isinstance(a, TAG_List) and isinstance(b, TAG_List):
        if len(a.tags) != len(b.tags):
            return False
        return all(nbt_equal(x, y) for x, y in zip(a.tags, b.tags))
    if isinstance(a, (TAG_Int_Array, TAG_Byte_Array, TAG_Long_Array)):
        return type(a) is type(b) and list(a.value) == list(b.value)
    return getattr(a, 'value', a) == getattr(b, 'value', b)


def _collect_bes(world):
    out = {}
    rf = region.RegionFile(str(Path(world) / 'region' / 'r.0.0.mca'))
    for cx, cz in ((0, 0), (0, 1)):
        try:
            ch = rf.get_chunk(cx, cz)
        except Exception:
            continue
        if ch is None:
            continue
        bes = cget(ch, 'block_entities')
        if bes is None:
            continue
        for be in bes.tags:
            tid, x, y, z = cget(be, 'id'), cget(be, 'x'), cget(be, 'y'), cget(be, 'z')
            if tid and x and y and z:
                out[(tid.value, x.value, y.value, z.value)] = be
    return out


def _collect_entities(world):
    out = []
    rf = region.RegionFile(str(Path(world) / 'entities' / 'r.0.0.mca'))
    for cx, cz in ((0, 0), (0, 1)):
        try:
            ch = rf.get_chunk(cx, cz)
        except Exception:
            continue
        if ch is None:
            continue
        ents = cget(ch, 'Entities')
        if ents is None:
            continue
        out.extend(ents.tags)
    return out


def _path(tag, *keys):
    for k in keys:
        tag = cget(tag, k)
        if tag is None:
            return None
    return tag


def verify(workdir, ref, source=None):
    print(f"--- verify: {workdir}  vs  {ref} ---")
    wbe, rbe = _collect_bes(workdir), _collect_bes(ref)
    sbe = _collect_bes(source) if source else {}
    results = []

    def chk(label, ok, detail=''):
        results.append((label, ok, detail))

    # BE features: (id, list-of subpaths to compare). Only compares when ref
    # has the field. Each path is a tuple of keys; None means whole BE minus ids.
    be_paths = {
        'create:fluid_tank':    [('TankContent',)],
        'create:item_vault':    [('Inventory', 'Items')],
        'create:factory_panel': [('bottom_left', 'Targeting'), ('bottom_left', 'TargetedBy')],
        'create:table_cloth':   [('RequestData',)],
        'create:clipboard':     [('components', 'create:clipboard_content')],
        'create:funnel':        [('Filter',)],
        # State/Overlay are runtime values restored from source (the independent
        # target has its own snapshot), so only the structural binding is checked.
        'create:track_signal':  [('TargetTrack',)],
        'create:chain_conveyor': [('Connections',), ('Source',)],
        'create:package_postbox': [('Target',)],
    }
    for key, be in sorted(wbe.items()):
        bid = key[0]
        if bid not in be_paths:
            continue
        rb = rbe.get(key)
        if rb is None:
            continue
        for p in be_paths[bid]:
            rv = _path(rb, *p)
            if rv is None or (isinstance(rv, TAG_List) and len(rv.tags) == 0):
                continue  # nothing meaningful to compare on this object
            wv = _path(be, *p)
            label = f"{bid.split(':')[1]}{key[1:]}.{'.'.join(p)}"
            chk(label, nbt_equal(wv, rv), '' if wv is not None else 'MISSING')

    # Tracks: compare Positions against SOURCE (target's track-graph nodes sit at
    # different coords because it was laid independently, so target is no oracle).
    for key, be in sorted(wbe.items()):
        if key[0] != 'create:track':
            continue
        sb = sbe.get(key)
        if sb is None:
            continue
        ok = True
        wconns, sconns = cget(be, 'Connections'), cget(sb, 'Connections')
        for wc, sc in zip(wconns.tags, sconns.tags):
            wp, sp = cget(wc, 'Positions'), cget(sc, 'Positions')
            if wp is None or sp is None:
                continue
            for wpos, spos in zip(wp.tags, sp.tags):
                wia = cget(wpos, 'Pos')
                sx, sy, sz = cget(spos, 'X'), cget(spos, 'Y'), cget(spos, 'Z')
                if sx is None:
                    continue
                if wia is None or list(wia.value) != [sx.value, sy.value, sz.value]:
                    ok = False
        chk(f"track{key[1:]}.Positions", ok)

    # Entities: paintings (by TileX/Y/Z), item-frame printouts, package (Box)
    paint_ref, frame_ref, pkg_ref = {}, {}, None
    for e in _collect_entities(ref):
        tid = cget(e, 'id')
        if tid is None:
            continue
        if tid.value == 'immersive_paintings:painting':
            paint_ref[(cget(e, 'TileX').value, cget(e, 'TileY').value, cget(e, 'TileZ').value)] = e
        elif tid.value in ('minecraft:item_frame', 'minecraft:glow_item_frame'):
            frame_ref[(cget(e, 'TileX').value, cget(e, 'TileY').value, cget(e, 'TileZ').value)] = e
        elif tid.value == 'create:package':
            pkg_ref = e
    for e in _collect_entities(workdir):
        tid = cget(e, 'id')
        if tid is None:
            continue
        if tid.value == 'immersive_paintings:painting':
            k = (cget(e, 'TileX').value, cget(e, 'TileY').value, cget(e, 'TileZ').value)
            r = paint_ref.get(k)
            if r is not None:
                ok = (nbt_equal(cget(e, 'Facing'), cget(r, 'Facing')) and
                      nbt_equal(cget(e, 'Motive'), cget(r, 'Motive')) and
                      nbt_equal(cget(e, 'Pos'), cget(r, 'Pos')))
                chk(f"painting{k}", ok)
        elif tid.value in ('minecraft:item_frame', 'minecraft:glow_item_frame'):
            item = cget(e, 'Item')
            iid = cget(item, 'id') if item is not None else None
            if iid is None or 'computercraft' not in iid.value:
                continue
            k = (cget(e, 'TileX').value, cget(e, 'TileY').value, cget(e, 'TileZ').value)
            r = frame_ref.get(k)
            if r is not None:
                wv = _path(item, 'components', 'computercraft:printout')
                rv = _path(cget(r, 'Item'), 'components', 'computercraft:printout')
                chk(f"item_frame_printout{k}", nbt_equal(wv, rv), '' if wv is not None else 'MISSING')
        elif tid.value == 'create:package' and pkg_ref is not None:
            wv = _path(e, 'Box', 'components', 'create:package_contents')
            rv = _path(pkg_ref, 'Box', 'components', 'create:package_contents')
            chk("package.Box.contents", nbt_equal(wv, rv), '' if wv is not None else 'MISSING')

    # Backpack .dat: every stack should be count:Int (no leftover 1.20 Count)
    dat = Path(workdir) / 'data' / 'sophisticatedbackpacks.dat'
    if dat.exists():
        n = nbt_mod.NBTFile(str(dat))
        leftover = [0]

        def chkstack(item):
            if cget(item, 'Count') is not None:
                leftover[0] += 1
        walk_items(n, chkstack)
        chk("backpack.dat all stacks count:Int", leftover[0] == 0,
            '' if leftover[0] == 0 else f"{leftover[0]} stacks still 1.20 Count")

    # Worn backpack: relocated from the curios slot into a free main-inventory
    # slot (the accessories mod rejects a re-equipped backpack on load). Verify
    # each source-worn backpack now appears in the target Inventory carrying
    # storage_uuid == its original contentsUuid.
    pd = Path(workdir) / 'playerdata'
    spd = Path(source) / 'playerdata' if source else None
    if pd.exists() and spd is not None and spd.exists():
        for sf in sorted(spd.glob('*.dat')):
            sci = _path(nbt_mod.NBTFile(str(sf)), 'ForgeCaps', 'curios:inventory', 'Curios')
            if sci is None:
                continue
            worn = []
            for c in sci.tags:
                items = _path(c, 'StacksHandler', 'Stacks', 'Items')
                for it in (items.tags if items is not None else []):
                    iid = cget(it, 'id')
                    O = _path(it, 'tag', 'contentsUuid')
                    if iid is not None and is_backpack(iid.value) and O is not None:
                        worn.append(list(O.value))
            tf = pd / sf.name
            if not worn or not tf.exists():
                continue
            inv = cget(nbt_mod.NBTFile(str(tf)), 'Inventory')
            inv_uuids = [list(su.value) for it in (inv.tags if inv is not None else [])
                         if (su := _path(it, 'components', 'sophisticatedcore:storage_uuid')) is not None
                         and (iid := cget(it, 'id')) is not None and is_backpack(iid.value)]
            for O in worn:
                chk(f"worn backpack -> inventory ({sf.name[:8]})", O in inv_uuids,
                    '' if O in inv_uuids else 'not relocated into inventory')

    # Report
    npass = sum(1 for _, ok, _ in results if ok)
    for label, ok, detail in results:
        mark = 'PASS' if ok else 'FAIL'
        print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ''))
    print(f"  ===== {npass}/{len(results)} checks passed =====")
    if UNMAPPED_TAG_KEYS:
        print("  note: tag keys routed to custom_data:",
              ', '.join(f"{k}={v}" for k, v in UNMAPPED_TAG_KEYS.most_common(20)))


# ============================================================================
# Index-source preflight
# ============================================================================
def index_source(sources):
    print("--- index-source ---")
    print(f"  block_entities: {len(sources.be_by_xyz)}")
    by_id = Counter(k[0] for k in sources.be_by_xyz)
    for tid in ('create:chain_conveyor', 'create:track', 'create:track_signal',
                'sophisticatedbackpacks:backpack'):
        print(f"    {tid}: {by_id.get(tid, 0)}")
    print(f"  entities: {len(sources.ent_by_uuid)}")
    ent_ids = Counter()
    for ent in sources.ent_by_uuid.values():
        tid = cget(ent, 'id')
        if tid:
            ent_ids[tid.value] += 1
    for tid in ('immersive_paintings:painting', 'minecraft:item_frame',
                'minecraft:glow_item_frame'):
        print(f"    {tid}: {ent_ids.get(tid, 0)}")
    print(f"  playerdata files: {len(sources.playerdata)}")


# ============================================================================
# Parallel, single-pass, resumable migration (region workers + global pass)
# ============================================================================
# (subdir, label). Overworld subdir is '' -> Path(world)/'' == Path(world).
DIMENSIONS = [('', 'overworld'), ('DIM-1', 'nether'), ('DIM1', 'end')]
_DIM_LABEL = {sub: lbl for sub, lbl in DIMENSIONS}
_DIM_SUBDIR = {lbl: sub for sub, lbl in DIMENSIONS}
_DIM_SUBDIR['DIM-1'] = 'DIM-1'
_DIM_SUBDIR['DIM1'] = 'DIM1'


def _dim_base(world, dim_subdir):
    return Path(world) / dim_subdir if dim_subdir else Path(world)


def _rkey(dim_subdir, rx, rz):
    return f"{_DIM_LABEL.get(dim_subdir, dim_subdir or 'overworld')}.{rx}.{rz}"


class RegionSources:
    """Source index scoped to a single region: block entities by (id,x,y,z) and
    static entities by UUID, loaded only from that region's source files."""

    def __init__(self, source_world, dim_subdir, rx, rz):
        self.be_by_xyz = {}
        self.ent_by_uuid = {}
        base = _dim_base(source_world, dim_subdir)
        self._index(base / 'region' / f'r.{rx}.{rz}.mca', 'block_entities', self._add_be)
        self._index(base / 'entities' / f'r.{rx}.{rz}.mca', 'Entities', self._add_ent)

    def _index(self, path, list_key, add):
        if not path.exists():
            return
        try:
            rf = region.RegionFile(str(path))
        except Exception:
            return
        for entry in rf.get_chunk_coords():
            try:
                chunk = rf.get_chunk(entry['x'], entry['z'])
            except Exception:
                continue
            if chunk is None:
                continue
            lst = cget(chunk, list_key)
            if lst is None:
                continue
            for item in lst.tags:
                add(item)

    def _add_be(self, be):
        tid, x, y, z = cget(be, 'id'), cget(be, 'x'), cget(be, 'y'), cget(be, 'z')
        if tid and x and y and z:
            self.be_by_xyz[(tid.value, x.value, y.value, z.value)] = be

    def _add_ent(self, ent):
        u = cget(ent, 'UUID')
        if u:
            self.ent_by_uuid[tuple(u.value)] = ent

    def be(self, mc_id, x, y, z):
        return self.be_by_xyz.get((mc_id, x, y, z))

    def entity_by_uuid(self, uuid):
        return self.ent_by_uuid.get(tuple(uuid))


def _process_region_file(region_path, visitors, dry_run):
    """Single-pass over one region file: read each chunk once, run every visitor,
    write changed chunks once. Atomic on write (patch a temp copy, os.replace)."""
    region_path = Path(region_path)
    if not region_path.exists() or not visitors:
        return (0, 0)
    work = region_path
    if not dry_run:
        work = region_path.with_suffix('.mca.tmp')
        shutil.copy2(region_path, work)
    rf = region.RegionFile(str(work))
    n_chunks = n_mod = 0
    try:
        for entry in rf.get_chunk_coords():
            try:
                chunk = rf.get_chunk(entry['x'], entry['z'])
            except Exception:
                continue
            if chunk is None:
                continue
            n_chunks += 1
            dirty = False
            for v in visitors:
                if v(chunk):
                    dirty = True
            if dirty:
                n_mod += 1
                if not dry_run:
                    rf.write_chunk(entry['x'], entry['z'], chunk)
    finally:
        try:
            rf.close()
        except Exception:
            try:
                rf.file.close()
            except Exception:
                pass
    if not dry_run:
        _atomic_replace(str(work), str(region_path))
    return (n_chunks, n_mod)


def migrate_region(dim_subdir, rx, rz, source_world, target_world, dry_run=False):
    """Process one region (region/ + entities/) self-contained. Picklable worker.
    Returns a dict with stats and the backpack (S,O) pairs / claimed UUIDs that the
    global .dat pass needs."""
    UNMAPPED_TAG_KEYS.clear()  # per-region audit of item-NBT keys routed to custom_data
    src = RegionSources(source_world, dim_subdir, rx, rz)
    stats = Counter()
    pairs, claimed = [], []
    region_visitors = [
        make_block_renames_visitor(stats),
        make_chain_conveyors_visitor(src, stats),
        make_tracks_visitor(src, stats),
        make_track_signals_visitor(src, stats),
        make_be_fixer_visitor(src, stats, 'create:fluid_tank', _per_be_fluid_tank),
        make_be_fixer_visitor(src, stats, 'create:factory_panel', _per_be_factory_panel),
        make_be_fixer_visitor(src, stats, 'create:table_cloth', _per_be_table_cloth),
        make_be_fixer_visitor(src, stats, 'create:clipboard', _per_be_clipboard),
        make_be_fixer_visitor(src, stats, 'create:package_postbox', _per_be_postbox),
        make_inventories_visitor(src, stats),
        make_be_backpacks_visitor(src, stats),
        make_mod_items_visitor(src, stats),
        make_backpack_collect_visitor(stats, pairs, claimed),
    ]
    entity_visitors = [
        make_paintings_visitor(src, stats),
        make_item_frames_visitor(src, stats),
        make_package_entities_visitor(src, stats),
    ]
    base = _dim_base(target_world, dim_subdir)
    rc, rm = _process_region_file(base / 'region' / f'r.{rx}.{rz}.mca', region_visitors, dry_run)
    ec, em = _process_region_file(base / 'entities' / f'r.{rx}.{rz}.mca', entity_visitors, dry_run)
    stats['region_chunks'], stats['region_modified'] = rc, rm
    stats['entity_chunks'], stats['entity_modified'] = ec, em
    return {'dim': dim_subdir, 'rx': rx, 'rz': rz, 'stats': dict(stats),
            'backpack_pairs': pairs, 'backpack_claimed': claimed,
            'unmapped': dict(UNMAPPED_TAG_KEYS)}


def _migrate_region_worker(args):
    """ProcessPool entry point. Catches errors so one bad region can't kill the run."""
    dim_subdir, rx, rz, source_world, target_world, dry_run = args
    try:
        res = migrate_region(dim_subdir, rx, rz, source_world, target_world, dry_run)
        res['ok'] = True
        return res
    except Exception:
        return {'dim': dim_subdir, 'rx': rx, 'rz': rz, 'ok': False,
                'error': traceback.format_exc(), 'stats': {}, 'backpack_pairs': [], 'backpack_claimed': []}


# --- global pass (.dat + playerdata; the only non-region state) -------------
def _global_backpacks(source_world, target_world, region_pairs, region_claimed, dry_run):
    datpath = Path(target_world) / 'data' / 'sophisticatedbackpacks.dat'
    if not datpath.exists():
        print("  (no sophisticatedbackpacks.dat)")
        return
    dat = nbt_mod.NBTFile(str(datpath))
    bc = _path(dat, 'data', 'backpackContents')
    if bc is None:
        return
    idx = {tuple(cget(e, 'uuid').value): e for e in bc.tags if cget(e, 'uuid') is not None}
    stats = Counter()
    claimed = set(tuple(c) for c in region_claimed)
    dat_changed = [False]

    # 1. rehome contents for backpack items found across all regions
    for S, O in region_pairs:
        claimed.add(tuple(S))
        if _bp_rehome(idx, bc, S, O, stats):
            dat_changed[0] = True

    # 2. playerdata backpacks (inventory/ender) + worn restore
    src_pd = {}
    spd = Path(source_world) / 'playerdata'
    if spd.exists():
        for f in spd.glob('*.dat'):
            try:
                src_pd[f.name] = nbt_mod.NBTFile(str(f))
            except Exception:
                pass
    overflow = []  # (item_1_21, pos_xyz, dim_str) for backpacks that couldn't fit in inventory
    pd = Path(target_world) / 'playerdata'
    if pd.exists():
        for f in sorted(pd.glob('*.dat')):
            try:
                n = nbt_mod.NBTFile(str(f))
            except Exception as e:
                print(f"  !! open {f.name}: {e}")
                continue
            fc = [False]

            def on_item(it):
                idt = cget(it, 'id')
                if idt is not None and isinstance(idt.value, str) and is_backpack(idt.value):
                    if _bp_handle_present(it, idx, bc, claimed, stats):
                        dat_changed[0] = True
                        fc[0] = True

            walk_items(n, on_item)
            src_nbt = src_pd.get(f.name)
            if src_nbt is not None and _bp_restore_worn(n, src_nbt, idx, bc, claimed, stats,
                                                        overflow=overflow):
                fc[0] = True
                dat_changed[0] = True
            if fc[0]:
                print(f"  {f.name}: {'rewriting' if not dry_run else 'WOULD rewrite'}")
                if not dry_run:
                    n.write_file(str(f))

    # Spawn worn backpacks that couldn't fit into a full inventory as ground entities.
    for item, pos_xyz, dim_str in overflow:
        _spawn_item_in_entities(target_world, dim_str, pos_xyz, item, dry_run, stats)

    if dat_changed[0]:
        print(f"  sophisticatedbackpacks.dat: {'rewriting' if not dry_run else 'WOULD rewrite'}")
        if not dry_run:
            dat.write_file(str(datpath))
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


def migrate_global(source_world, target_world, region_pairs, region_claimed, dry_run=False):
    """The only serial / world-global work: sophisticatedbackpacks.dat and playerdata."""
    print("--- migrate_global (.dat + playerdata) ---")
    fix_backpack_dat(target_world, dry_run)
    _global_backpacks(source_world, target_world, region_pairs, region_claimed, dry_run)
    stats = fix_mod_items_playerdata(target_world, dry_run)
    for k, v in sorted(stats.items()):
        print(f"  mod_items {k}: {v}")


# --- region discovery + manifest --------------------------------------------
_RE_REGION = re.compile(r'^r\.(-?\d+)\.(-?\d+)\.mca$')


def discover_regions(target_world, only=None):
    regions = []
    for dim_subdir, _lbl in DIMENSIONS:
        rdir = _dim_base(target_world, dim_subdir) / 'region'
        if not rdir.exists():
            continue
        for f in sorted(rdir.glob('r.*.mca')):
            m = _RE_REGION.match(f.name)
            if m:
                regions.append((dim_subdir, int(m.group(1)), int(m.group(2))))
    if only:
        only = set(only)
        regions = [r for r in regions if _rkey(*r) in only]
    return regions


def _atomic_replace(tmp, dst, attempts=20, delay=0.1):
    """os.replace, retrying transient Windows locks (Defender/indexer)."""
    for i in range(attempts):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def _manifest_path(target_world):
    return Path(target_world) / 'migration_manifest.json'


def load_manifest(target_world):
    p = _manifest_path(target_world)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {'regions': {}, 'global': {}}


def save_manifest(target_world, manifest):
    p = _manifest_path(target_world)
    tmp = p.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(manifest, indent=1))
    _atomic_replace(tmp, p)


def run_parallel(source_world, target_world, jobs=20, force=False, only=None,
                 dry_run=False, do_global=True):
    regions = discover_regions(target_world, only)
    manifest = load_manifest(target_world)
    todo = [r for r in regions
            if force or manifest['regions'].get(_rkey(*r), {}).get('status') != 'done']
    by_dim = Counter(_DIM_LABEL.get(d, d or 'overworld') for d, _rx, _rz in regions)
    print(f"=== run: {len(regions)} region(s) "
          f"[{', '.join(f'{k}: {v}' for k, v in by_dim.items()) or 'none found'}], "
          f"{len(todo)} to process, {len(regions) - len(todo)} already done, "
          f"jobs={jobs}, {'DRY-RUN' if dry_run else 'WRITE'} ===")
    if not regions:
        print("  !! no region files discovered under region/, DIM-1/region/, DIM1/region/ "
              "— check the world path / dimension layout")

    t0 = time.time()
    tasks = [(dim, rx, rz, source_world, target_world, dry_run) for (dim, rx, rz) in todo]
    done = 0
    if jobs > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            for res in ex.map(_migrate_region_worker, tasks):
                done += 1
                _record_region(manifest, target_world, res, dry_run, done, len(tasks))
    else:
        for t in tasks:
            res = _migrate_region_worker(t)
            done += 1
            _record_region(manifest, target_world, res, dry_run, done, len(tasks))

    print(f"=== region pass: {done} processed in {time.time() - t0:.1f}s ===")

    if do_global:
        all_pairs, all_claimed = [], []
        for info in manifest['regions'].values():
            if info.get('status') == 'done':
                all_pairs += info.get('backpack_pairs', [])
                all_claimed += info.get('backpack_claimed', [])
        migrate_global(source_world, target_world, all_pairs, all_claimed, dry_run)
        manifest['global'] = {'status': 'done', 'finished_at': time.time()}
        if not dry_run:
            save_manifest(target_world, manifest)

    # Aggregate the item-NBT audit across all regions (keys routed to custom_data)
    unmapped = Counter()
    for info in manifest['regions'].values():
        for k, v in (info.get('unmapped') or {}).items():
            unmapped[k] += v
    if unmapped:
        print("=== item-NBT keys routed to custom_data (review before trusting) ===")
        for k, v in unmapped.most_common():
            print(f"  {k}: {v}")
    print(f"=== run complete in {time.time() - t0:.1f}s ===")


def _record_region(manifest, target_world, res, dry_run, done, total):
    key = _rkey(res['dim'], res['rx'], res['rz'])
    if not res.get('ok'):
        print(f"  [FAIL {done}/{total}] {key}\n{res.get('error', '')}")
        manifest['regions'][key] = {'status': 'failed', 'error': res.get('error', '')[-500:]}
    else:
        st = res['stats']
        manifest['regions'][key] = {
            'status': 'done', 'stats': st, 'finished_at': time.time(),
            'backpack_pairs': res['backpack_pairs'], 'backpack_claimed': res['backpack_claimed'],
            'unmapped': res.get('unmapped', {}),
        }
        mod = st.get('region_modified', 0) + st.get('entity_modified', 0)
        if mod:
            print(f"  [done {done}/{total}] {key}  chunks={st.get('region_chunks', 0)} modified={mod}")
    if not dry_run:
        save_manifest(target_world, manifest)


def parse_coord(s):
    """'overworld.3.3' / 'DIM-1.0.-1' -> (dim_subdir, rx, rz)."""
    dim, rx, rz = s.rsplit('.', 2)
    return (_DIM_SUBDIR.get(dim, dim if dim != 'overworld' else ''), int(rx), int(rz))


# ============================================================================
# CLI
# ============================================================================
SUBCOMMANDS = (
    'index-source', 'fix-chain-conveyors', 'fix-tracks', 'fix-track-signals',
    'fix-paintings', 'fix-item-frames', 'fix-be-backpacks', 'fix-inv-backpacks',
    'fix-inventories', 'fix-fluid-tanks', 'fix-factory-panels', 'fix-table-cloths',
    'fix-clipboards', 'fix-mod-items', 'fix-backpack-dat', 'fix-worn-curios',
    'fix-backpacks', 'fix-package-entities', 'fix-postboxes', 'fix-block-renames',
    'fix-all', 'verify',
    'region', 'run', 'global',
)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('subcommand', choices=SUBCOMMANDS)
    ap.add_argument('--source', help='1.20 source world dir (required for fixers)')
    ap.add_argument('--target', required=True, help='1.21 target world dir to patch')
    ap.add_argument('--ref', help='1.21 reference world (for verify)')
    ap.add_argument('--write', action='store_true', help='Actually write changes (default: dry-run)')
    ap.add_argument('--coord', help="region subcommand: <dim>.<rx>.<rz>, e.g. overworld.3.3")
    ap.add_argument('--jobs', type=int, default=20, help="run: parallel worker processes (default 20)")
    ap.add_argument('--only', nargs='*', help="run: only these region keys (targeted rerun)")
    ap.add_argument('--force', action='store_true', help="run: reprocess regions even if manifest says done")
    ap.add_argument('--no-global', action='store_true', help="run: skip the .dat/playerdata global pass")
    args = ap.parse_args()

    if args.subcommand == 'verify':
        if not args.ref:
            ap.error('verify requires --ref <1.21-target world>')
        verify(args.target, args.ref, args.source)
        return

    # Parallel / per-region commands (use region-scoped source loading, not whole-world).
    if args.subcommand in ('region', 'run', 'global'):
        if not args.source:
            ap.error('this subcommand requires --source')
        dry = not args.write
        if dry:
            print("*** DRY RUN — pass --write to actually modify the target world ***\n")
        if args.subcommand == 'region':
            if not args.coord:
                ap.error('region requires --coord <dim>.<rx>.<rz>')
            dim, rx, rz = parse_coord(args.coord)
            res = migrate_region(dim, rx, rz, args.source, args.target, dry)
            print(f"--- region {_rkey(dim, rx, rz)} ---")
            for k, v in sorted(res['stats'].items()):
                if v:
                    print(f"  {k}: {v}")
            print(f"  backpack_pairs collected: {len(res['backpack_pairs'])}")
        elif args.subcommand == 'global':
            mani = load_manifest(args.target)
            pairs, claimed = [], []
            for info in mani['regions'].values():
                if info.get('status') == 'done':
                    pairs += info.get('backpack_pairs', [])
                    claimed += info.get('backpack_claimed', [])
            migrate_global(args.source, args.target, pairs, claimed, dry)
        else:  # run
            run_parallel(args.source, args.target, jobs=args.jobs, force=args.force,
                         only=args.only, dry_run=dry, do_global=not args.no_global)
        return

    dry = not args.write
    if dry:
        print("*** DRY RUN — pass --write to actually modify the target world ***\n")

    # Block-palette renames use a static map, so they don't need the 1.20 source.
    if args.subcommand == 'fix-block-renames':
        fix_block_renames(args.target, dry)
        return

    if not args.source:
        ap.error('this subcommand requires --source')
    sources = Sources(args.source)

    if args.subcommand == 'index-source':
        index_source(sources)
    elif args.subcommand == 'fix-chain-conveyors':
        fix_chain_conveyors(args.target, sources, dry)
    elif args.subcommand == 'fix-tracks':
        fix_tracks(args.target, sources, dry)
    elif args.subcommand == 'fix-track-signals':
        fix_track_signals(args.target, sources, dry)
    elif args.subcommand == 'fix-paintings':
        fix_paintings(args.target, sources, dry)
    elif args.subcommand == 'fix-item-frames':
        fix_item_frames(args.target, sources, dry)
    elif args.subcommand == 'fix-be-backpacks':
        fix_be_backpacks(args.target, sources, dry)
    elif args.subcommand == 'fix-inv-backpacks':
        fix_inv_backpacks(args.target, sources, dry)
    elif args.subcommand == 'fix-inventories':
        fix_inventories(args.target, sources, dry)
    elif args.subcommand == 'fix-fluid-tanks':
        fix_fluid_tanks(args.target, sources, dry)
    elif args.subcommand == 'fix-factory-panels':
        fix_factory_panels(args.target, sources, dry)
    elif args.subcommand == 'fix-table-cloths':
        fix_table_cloths(args.target, sources, dry)
    elif args.subcommand == 'fix-clipboards':
        fix_clipboards(args.target, sources, dry)
    elif args.subcommand == 'fix-mod-items':
        fix_mod_items(args.target, sources, dry)
    elif args.subcommand == 'fix-backpack-dat':
        fix_backpack_dat(args.target, dry)
    elif args.subcommand == 'fix-worn-curios':
        fix_worn_curios(args.target, sources, dry)
    elif args.subcommand == 'fix-backpacks':
        fix_backpacks(args.target, sources, dry)
    elif args.subcommand == 'fix-package-entities':
        fix_package_entities(args.target, sources, dry)
    elif args.subcommand == 'fix-postboxes':
        fix_postboxes(args.target, sources, dry)
    elif args.subcommand == 'fix-all':
        # mod block-ID renames in chunk palettes (must precede live load)
        fix_block_renames(args.target, dry)
        # structural fixers (reshaped/wiped payloads)
        fix_chain_conveyors(args.target, sources, dry)
        fix_tracks(args.target, sources, dry)
        fix_track_signals(args.target, sources, dry)
        fix_paintings(args.target, sources, dry)
        fix_fluid_tanks(args.target, sources, dry)
        fix_factory_panels(args.target, sources, dry)
        fix_table_cloths(args.target, sources, dry)
        fix_clipboards(args.target, sources, dry)
        fix_postboxes(args.target, sources, dry)
        fix_item_frames(args.target, sources, dry)
        fix_package_entities(args.target, sources, dry)
        # generic mod-inventory count/format sweep (region BEs)
        fix_inventories(args.target, sources, dry)
        # mod-items in vanilla containers (custom_data -> components)
        fix_mod_items(args.target, sources, dry)
        # backpacks: placed BE, then convert .dat item formats, then rehome
        # contents to each item's storage UUID + re-equip the worn one.
        fix_be_backpacks(args.target, sources, dry)
        fix_backpack_dat(args.target, dry)
        fix_backpacks(args.target, sources, dry)
    else:
        ap.error(f'Unknown subcommand: {args.subcommand}')


if __name__ == '__main__':
    main()
