import csv
import logging
import re
from pathlib import Path

# Tag list lives next to the pack root, see danbooru/README.md for its source.
CSV_DIR = Path(__file__).resolve().parent.parent / "danbooru"

CATEGORY_NAMES = {"0": "general", "1": "artist", "3": "copyright",
                  "4": "character", "5": "meta"}

# Parentheses and brackets are weighting syntax in A1111/ComfyUI prompts, so tags
# like shower_(place) or hatsune_miku_(vocaloid) have to reach the encoder escaped.
PROMPT_SYNTAX = re.compile(r"([()\[\]])")

# Word endings tried when an exact spelling misses, e.g. "tiled floor" -> tile_floor.
ENDINGS = ("ing", "ed", "es", "s")

# Phrasings the LLM likes that are not Danbooru tags. Left of "=" is what the model
# writes, right side is the real tag(s) - comma separated, so one phrase can expand
# into two tags. Editable in the node.
DEFAULT_ALIAS_MAP = """\
looking up at viewer = looking at viewer, looking up
high angle shot = from above
black nail polish = black nails
black toenail polish = toenail polish
black rimmed glasses = glasses
gray bath mat = mat
glass shower stall = shower (place)
star makeup = facepaint
slender body = skinny"""


def normalize_tag(tag):
    """Fold case, underscores, hyphens and spacing so spellings compare equal."""
    return re.sub(r"\s+", " ", tag.replace("\\", "").lower().replace("_", " ").replace("-", " ")).strip()


def loose_forms(tag):
    """Spellings of the same phrase with word endings dropped, spaces squeezed out.

    Returns a small set of candidates because the correct stem is not knowable:
    "tiled floor" yields tilefloor as well as tiledfloor and tilfloor.
    """
    words = normalize_tag(tag).split()
    variants = []
    for word in words:
        forms = {word}
        for ending in ENDINGS:
            if word.endswith(ending) and len(word) > len(ending) + 1:
                forms.add(word[: -len(ending)])
        if word.endswith("ed"):            # tiled -> tile
            forms.add(word[:-1])
        variants.append(sorted(forms))

    combined = {""}
    for forms in variants:
        combined = {prefix + form for prefix in combined for form in forms}
        if len(combined) > 64:
            return set()
    return combined


def escape_prompt_syntax(tag):
    return PROMPT_SYNTAX.sub(r"\\\1", tag)


def pick_csvs(csv_path):
    """Tag lists to load. Empty = every danbooru*.csv in the pack's folder, newest first.

    More than one list is normal: newer dumps drop tags that older checkpoints still
    know (silver_hair, bangs, uniform), so the union keeps both generations usable.
    """
    if csv_path.strip():
        paths = [Path(part.strip()) for part in re.split(r"[;\n]", csv_path) if part.strip()]
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(f"BooruTagNormalize: tag list not found: {path}")
        return paths
    paths = sorted(CSV_DIR.glob("danbooru*.csv"), reverse=True)
    if not paths:
        raise FileNotFoundError(
            f"BooruTagNormalize: no danbooru*.csv in {CSV_DIR}")
    return paths


def load_tag_lists(paths):
    """Return (exact, aliases, loose); earlier lists win, so pass the newest one first."""
    exact, aliases, loose = {}, {}, {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for row in csv.reader(f):
                if not row or not row[0].strip():
                    continue
                name = row[0].strip()
                posts = int(row[2]) if len(row) > 2 and row[2].strip().isdigit() else 0
                key = normalize_tag(name)
                exact.setdefault(key, (name, row[1].strip() if len(row) > 1 else "", posts))
                loose.setdefault("".join(key.split()), name)
                if len(row) > 3 and row[3].strip():
                    for alias in row[3].split(","):
                        if alias.strip():
                            aliases.setdefault(normalize_tag(alias), name)
    return exact, aliases, loose


def parse_alias_map(text):
    mapping, loose = {}, {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        source, target = line.split("=", 1)
        tags = [tag.strip() for tag in target.split(",") if tag.strip()]
        if source.strip() and tags:
            key = normalize_tag(source)
            mapping[key] = tags
            loose.setdefault("".join(key.split()), tags)
    return mapping, loose


class BooruTagNormalize:
    """Rewrite an LLM's tag output into tags that exist in the local Danbooru list."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"default": "", "multiline": True,
                                    "tooltip": "Tag string from the instruct node. Type here to test the node on its own."}),
                "csv_path": ("STRING", {"default": "",
                                        "tooltip": "Empty = every danbooru*.csv in the pack's danbooru folder. Separate several files with ; to pin an exact set."}),
                "min_posts": ("INT", {"default": 0, "min": 0, "max": 10000000, "step": 100,
                                      "tooltip": "Drop tags with fewer Danbooru posts than this. 0 keeps all."}),
                "drop_categories": ("STRING", {"default": "artist",
                                               "tooltip": "Comma separated tag categories to drop: artist, character, copyright, meta."}),
                "alias_map": ("STRING", {"default": DEFAULT_ALIAS_MAP, "multiline": True,
                                         "tooltip": "One per line, 'what the model writes = real tag(s)'. Empty falls back to these defaults; a single # turns every mapping off."}),
                "escape_parens": ("BOOLEAN", {"default": True,
                                              "tooltip": "Escape ( ) [ ] so the prompt encoder reads them as text. Turn off for raw Danbooru spelling."}),
                "keep_unknown": ("BOOLEAN", {"default": True,
                                             "tooltip": "Keep tags the word list does not have instead of dropping them. They are listed on the unknown output."}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("tags", "dropped", "unknown", "report")
    FUNCTION = "normalize"
    CATEGORY = "prompt/booru"
    DESCRIPTION = ("Canonicalizes an LLM's tag output against the local Danbooru tag list: "
                   "exact match, alias, word-form, then the editable phrase map. Tags the list "
                   "does not have are kept as written unless keep_unknown is off, so nothing in "
                   "the description gets lost. Tags are written with spaces and their brackets "
                   "escaped so the prompt encoder reads them as text.")
    SEARCH_ALIASES = ["danbooru", "booru tags", "tag validate", "标签归一", "标签校验"]

    def normalize(self, text, csv_path, min_posts, drop_categories, alias_map,
                  escape_parens=True, keep_unknown=True):
        if not alias_map.strip():
            alias_map = DEFAULT_ALIAS_MAP
        paths = pick_csvs(csv_path)
        exact, aliases, loose = load_tag_lists(paths)
        phrases, loose_phrases = parse_alias_map(alias_map)
        blocked = {name.strip().lower() for name in drop_categories.split(",") if name.strip()}

        kept, dropped, unknown, seen = [], [], [], set()
        counts = {"exact": 0, "alias": 0, "phrase": 0, "loose": 0, "unknown": 0, "filtered": 0}
        for raw in re.split(r"[,\n]", text or ""):
            raw = raw.strip()
            if not raw:
                continue
            key = normalize_tag(raw)
            # Aliases are checked before exact matches: a few tags exist both as their
            # own row and as an alias of a newer tag, and those should be canonicalized.
            if key in aliases:
                tags, how = [aliases[key]], "alias"
            elif key in exact:
                tags, how = [exact[key][0]], "exact"
            elif key in phrases:
                tags, how = phrases[key], "phrase"
            elif key.endswith("s") and key[:-1] in exact:
                tags, how = [exact[key[:-1]][0]], "exact"
            else:
                tags, how = None, "loose"
                for form in sorted(loose_forms(raw)):
                    if form in loose:
                        tags = [loose[form]]
                        break
                    if form in loose_phrases:
                        tags = loose_phrases[form]
                        break
                if tags is None:
                    if not keep_unknown:
                        dropped.append(raw)
                        continue
                    tags, how = [raw], "unknown"

            counts[how] += 1
            for tag in tags:
                info = exact.get(normalize_tag(tag))
                # Danbooru stores underscores, prompts read better with spaces.
                name = (info[0] if info else tag).replace("_", " ")
                if info and (info[2] < min_posts or CATEGORY_NAMES.get(info[1], info[1]) in blocked):
                    dropped.append(name)
                    counts["filtered"] += 1
                    continue
                key = normalize_tag(name)
                if key in seen:
                    continue
                seen.add(key)
                kept.append(escape_prompt_syntax(name) if escape_parens else name)
                if how == "unknown":
                    unknown.append(name)

        report = ("%s | exact %d / alias %d / phrase %d / loose %d / unknown %d / filtered %d"
                  " -> kept %d, dropped %d"
                  % ("+".join(path.name for path in paths),
                     counts["exact"], counts["alias"], counts["phrase"],
                     counts["loose"], counts["unknown"], counts["filtered"],
                     len(kept), len(dropped)))
        if dropped:
            report += " | dropped: " + ", ".join(dropped)
        if unknown:
            report += " | not in word list: " + ", ".join(unknown)
        logging.info("[BooruTagNormalize] %s", report)
        return (", ".join(kept), ", ".join(dropped), ", ".join(unknown), report)


NODE_CLASS_MAPPINGS = {"BooruTagNormalize": BooruTagNormalize}

NODE_DISPLAY_NAME_MAPPINGS = {"BooruTagNormalize": "Booru 标签归一 (Danbooru Tag Normalize)"}
