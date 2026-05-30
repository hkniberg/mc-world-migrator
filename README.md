# MC 1.20.1 (Forge) → 1.21.1 (NeoForge) World Migrator

A repair tool for Minecraft worlds that break when a **1.20.1 / Forge** modpack save
is opened in **1.21.1 / NeoForge**. It targets a Create-based modpack
(Create, Create: Steam 'n' Rails, Sophisticated Backpacks, Immersive Paintings,
CC: Tweaked) but the core item-conversion logic is generic.

## The problem

When the old world is opened in 1.21, vanilla's DataFixerUpper and the mods' own
fixers convert the data that lives in **vanilla** containers (chests, barrels,
player inventory, ender chests) correctly — but they do **not** reach data stored
**inside mod blocks/items**. The result is one root cause with two symptoms:

1. **Counts reset to 1.** Item stacks inside mod inventories (item vaults, funnels,
   backpacks, …) lose their stack size — `Count` is dropped and the reader defaults
   to 1.
2. **Reshaped payloads are wiped or defaulted.** Where a mod renamed/restructured
   its NBT (fluid tanks, factory-gauge links, shops, clipboards, paintings, …),
   migration writes an empty default instead of converting.

This tool repairs the migrated world by reading the **original 1.20 save as the
source of truth** and rewriting each affected block/entity/item/`.dat` record into
the correct 1.21 schema, keyed by block position, entity UUID, or storage UUID.

> Because the broken world no longer contains the lost values, **you must keep your
> original 1.20 save** — the tool reads from it.

## What it fixes

| Area | Detail |
|---|---|
| **Item counts** | Every stack in every mod inventory, world-wide (vaults, funnels, toolboxes, packagers, backpacks, shop displays, filters, …) |
| **Sophisticated Backpacks** | Placed, inventory, and (best-effort) worn backpacks; contents rehomed to each backpack's storage UUID in `sophisticatedbackpacks.dat`; nested backpacks |
| **Create chain conveyors** | Chain connections restored |
| **Create tracks** | Connection positions incl. **curved/bezier** segments |
| **Create track signals** | `TargetTrack` binding + state restored (see limitations) |
| **Create fluid tanks** | Fluid contents (`{Amount,FluidName}` → `TankContent.Fluid{amount,id}`) |
| **Create factory gauges** | Inter-panel connections (`Targeting`/`TargetedBy`) |
| **Create shops (table cloths)** | Encoded request / offer / target restored |
| **Create clipboards** | Page content |
| **Create packages** | Placed packages' contents (inventory packages: see limitations) |
| **Create postboxes** | Linked station target |
| **Item / attribute filters** | `create:filter` and `create:attribute_filter` contents and settings |
| **Immersive Paintings** | Orientation (facing enum remap — fixes wall→floor) and image (motive id) |
| **CC: Tweaked printouts** | Text in item frames |
| **Complex item NBT inside mod containers** | Enchantments, custom names, lore, damage, **written/writable books**, **shulker boxes / containers (recursive)**, **banners & shields** (pattern + colour), potions, trims, etc. |

Anything the item converter doesn't have an explicit rule for is preserved under
`minecraft:custom_data` (exactly what vanilla does) and **logged**, so nothing is
silently lost — see "Auditing" below.

## What it does NOT fix (known limitations)

- **Train signals' live track binding.** The track graph migrates, but the signal's
  runtime edge-binding does not reliably reconnect. Easiest fix: **break and
  re-place the signal blocks in-game** (a few seconds each). Editing the track graph
  directly risks derailing a working train, so the tool leaves it alone.
- **Worn backpacks.** The Curios→Accessories migration drops the equipped item;
  the tool re-inserts it and restores its contents, but the accessories mod may
  still reject it on load. **Recommended: have players unequip backpacks before
  migrating.**
- **Loose packages in the player inventory.** Create culls package *items* on load.
  Placed packages in the world are fine.
- **A few uncommon item components inside mod containers** are preserved in
  `custom_data` but not yet mapped to their native 1.21 component: loaded crossbows
  (`Charged`), player heads (`SkullOwner`), `HideFlags`. The items survive but may
  miss that one feature. *(Items in vanilla containers are unaffected — vanilla
  handles them completely.)*

## Requirements

- Python 3.9+
- The `NBT` library: `pip install -r requirements.txt`

## How to run (the real migration)

1. **Back up everything.** Always work on **copies** of your saves.
2. Keep your **original 1.20.1 (Forge) save** — it is the source of truth.
3. **Force-upgrade the whole world in 1.21.1 / NeoForge** so *every* chunk is
   converted to the new format — then quit and make a copy of that upgraded save
   to patch.

   > ⚠️ **Do not just log in and walk around.** Vanilla's DataFixerUpper upgrades a
   > chunk only when that chunk is *loaded*, so logging in converts only the chunks
   > near you and leaves every distant chunk in the old 1.20 format. This tool writes
   > 1.21-schema data; if it patches a chunk that is still at the old `DataVersion`,
   > Minecraft will later re-run its fixers on that chunk when it finally loads and
   > can **re-break** what this tool just fixed. Upgrade everything first.

   Use the real full-upgrade path:
   - **Singleplayer:** world list → select world → **Edit → Optimize World**.
   - **Dedicated server:** launch once with `--forceUpgrade`, e.g.
     `java -jar neoforge-server.jar --forceUpgrade --nogui`, and let it finish.

   Both use Minecraft's `WorldUpgrader`: it walks **every region file** in every
   dimension and re-saves every stored chunk at the current `DataVersion` (it does
   **not** generate new chunks). **Verify** it reached the far corners before
   continuing — spot-check the `DataVersion` of a chunk in a region you never
   personally visited; in 1.21.1 it should read **3955** (1.20.1 was **3465**).
4. *(Optional but recommended for big worlds)* trim the upgraded copy to the chunks
   you actually keep (e.g. with MCASelector); discarded chunks regenerate fresh in
   1.21.
5. Run the repair with `run` (parallel, resumable; default is a safe **dry-run**,
   add `--write` to apply):

   ```bash
   # dry-run first — processes regions, prints the custom_data audit, writes nothing
   python migrate_world.py run --source /path/to/1.20-original --target /path/to/migrated-copy --jobs 20

   # then apply
   python migrate_world.py run --source /path/to/1.20-original --target /path/to/migrated-copy --jobs 20 --write
   ```

   On Windows (PowerShell), quote paths with spaces:

   ```powershell
   python migrate_world.py run --source "C:\saves\1.20-original" --target "C:\saves\migrated-copy" --jobs 20 --write
   ```

6. Open the patched copy in 1.21. Re-place any train signals.

The tool is **idempotent** — running it again changes nothing.

### Primary commands

```
run    parallel migration of the whole world across all dimensions, then the
       global .dat/playerdata pass. Resumable (see below). Flags:
         --jobs N      worker processes (default 20)
         --only K ...  only these region keys, e.g. overworld.0.0 DIM-1.1.-2
         --force       reprocess regions even if the manifest says done
         --no-global   skip the .dat/playerdata pass
region migrate a single region for testing / targeted rerun:
         --coord <dim>.<rx>.<rz>   e.g. overworld.3.3   (dims: overworld, DIM-1, DIM1)
global only the .dat + playerdata pass (uses pairs recorded in the manifest)
verify dev/testing only — compares a patched world to a hand-built 1.21 reference
       (needs --ref); not meaningful for real migrations
fix-all single-threaded everything-in-one-process; fine for tiny worlds / fallback
```

`fix-*` subcommands (e.g. `fix-tracks`, `fix-paintings`, `fix-inventories`) run an
individual fixer over the whole world — handy for debugging.

### Resumability

`run` writes a `migration_manifest.json` in the target world and marks each region
done as it finishes (atomically). If it crashes after converting, say, 80 regions,
just run the same command again — it **skips the 80 done regions** and continues.
`--only <coord>` reprocesses specific regions; `--force` redoes everything. (Even a
reprocessed region is safe — the fixers are idempotent.)

### Auditing what falls through (recommended before a real run)

A `run` prints **"item-NBT keys routed to custom_data"** at the end — the complete
list of item-NBT keys in *your* world that aren't yet mapped to a native 1.21
component. If something valuable shows up, it can be added to the converter before
you commit with `--write`. For most worlds this list is short (crossbows / player
heads).

## How it works

`migrate_world.py` is self-contained (only depends on `NBT`). It:

- indexes the 1.20 source world (block entities by position, entities by UUID,
  playerdata);
- walks the target world's region/entities/`data`/`playerdata`;
- for each broken structure, restores the value from the source in the 1.21 schema;
- converts 1.20 item NBT (`Count`/`tag`) to 1.21 (`count`/`components`) via a
  mapping table with a `custom_data` catch-all, including mod-specific converters
  (Create filters/packages/clipboards, backpacks) and recursive container items.

## Diagnostics (`tools/`)

Read-only NBT inspectors used during development; handy for troubleshooting:

```bash
python tools/dump_chunk_nbt.py <world>/region "create:item_vault"   # dump matching block entities
python tools/find_items.py     <world> "backpack"                   # find items by id substring
```

## Performance

`run` reads each chunk **once**, applies all fixers, and writes it once, with one
worker **process per region** (regions are independent). On a 16-core/24-thread
desktop a ~8,700-chunk test world migrates in ~15 s (the single-threaded `fix-all`
took ~240 s for the same result — ~16× faster). Tune with `--jobs`. Memory is
modest: each worker only loads its own region's source index.

## Safety

- Default dry-run; `--write` required to modify anything.
- Only writes structures it understands; never deletes.
- Reads the 1.20 source read-only.
- **Still: back up, and run on copies.**
