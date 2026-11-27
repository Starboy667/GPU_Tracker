"""Starting points for a new watch. A preset only pre-fills the form: nothing is stored about it."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

DEALABS_GPU_FEED = "https://www.dealabs.com/rss/groupe/cartes-graphiques"
DEALABS_TV_FEED = "https://www.dealabs.com/rss/groupe/televiseurs"

USED_GOODS = r"occasion|reconditionn"
# Prebuilt PCs, laptops (16"), CPU+GPU bundles, waterblocks, used or refurbished cards.
GPU_EXCLUDE = (
    r'\bPC\b|portable|laptop|ordinateur|\d+(\.\d)?"|ryzen|core\s|ultra\s\d|\d+\s?go/|/\d+go|\+|\bkit\b'
    r"|watercooling|waterblock|alphacool|backplate|bundle|" + USED_GOODS
)
# Accessories and bundles that share a TV's model name.
TV_EXCLUDE = r"support|\bpied|c[âa]ble|t[ée]l[ée]commande|barre de son|\+|" + USED_GOODS

# What a PC player rarely wants from a key comparator: console versions, accounts instead of keys,
# boxed or second-hand copies, gifts tied to a region. Remove a word to let that kind back in.
PC_GAME_EXCLUDE = (
    r"PlayStation|\bPS[45]\b|Xbox|Switch|Nintendo|Vente de compte|\bcompte\b|\bGift\b|boîte|Occasion|"
    r"Rest of World|\[DLC\]|\[extra\]"
)

# The seven sites the original script knew: what a YAML document without `sites:` means.
ORIGINAL_SITES = ("ldlc", "materiel", "topachat", "grosbill", "alternate", "idealo", "dealabs")
ALL_SITES = ("ldlc", "materiel", "topachat", "grosbill", "alternate", "joybuy", "idealo", "dealabs")
GAME_SITES = ("steam", "dlcompare")
GENERAL_SITES = ("ldlc", "materiel", "joybuy", "idealo")


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    description: str
    example: str  # placeholder for the name field
    sites: tuple[str, ...]
    exclude: str = ""
    min_price: float | None = None
    site_urls: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    notify_restock: bool = False


PRESETS: tuple[Preset, ...] = (
    Preset(
        key="generic",
        label="Any product",
        description="Search the general shops and the Idealo price comparator by name.",
        example="Sony WH-1000XM6",
        sites=GENERAL_SITES,
        exclude=USED_GOODS,
    ),
    Preset(
        key="gpu",
        label="Graphics card",
        description="Every PC hardware shop, with filters against prebuilt PCs, laptops and bundles.",
        example="RTX 5070 Ti",
        sites=ALL_SITES,
        exclude=GPU_EXCLUDE,
        min_price=150,
        site_urls=MappingProxyType({"dealabs": DEALABS_GPU_FEED}),
    ),
    Preset(
        key="tv",
        label="TV or monitor",
        description="General shops plus the Dealabs TV feed, without stands, cables and soundbar bundles.",
        example="LG OLED55C5",
        sites=(*GENERAL_SITES, "dealabs"),
        exclude=TV_EXCLUDE,
        min_price=100,
        site_urls=MappingProxyType({"dealabs": DEALABS_TV_FEED}),
    ),
    Preset(
        key="game",
        label="Video game (PC)",
        description="Steam's own price next to the key shops compared by DLCompare, without consoles, accounts and boxed copies.",
        example="Elden Ring",
        sites=GAME_SITES,
        exclude=PC_GAME_EXCLUDE,
    ),
    Preset(
        key="page",
        label="A specific product page",
        description="Follow the price and stock of one page on any shop. No search, no filters.",
        example="PS5 Pro at Boulanger",
        sites=(),
        notify_restock=True,
    ),
)
PRESETS_BY_KEY: Mapping[str, Preset] = MappingProxyType({p.key: p for p in PRESETS})
DEFAULT_PRESET = PRESETS[0]
