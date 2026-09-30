#!/usr/bin/env python3
"""
apply_theme.py - render per-app color files from palette.json and reload apps.

    ./apply_theme.py                    render + reload (reuses the saved accent, if any)
    ./apply_theme.py --accent 0fff50    set + save the accent, then render, regen wallpaper, reload
    ./apply_theme.py --reset            forget the saved accent, go back to palette.json
    ./apply_theme.py --dry-run          render in memory, report what would change, write nothing
    ./apply_theme.py --diff             like --dry-run, plus a unified diff of every file that would change
    ./apply_theme.py --sddm             also render + install the SDDM theme.conf.user (uses sudo)
    ./apply_theme.py --limine           also update the Limine theme block + boot wallpaper (uses sudo)
    ./apply_theme.py --force            treat everything as changed (wallpaper + reloads)
    ./apply_theme.py --no-reload --no-wallpaper

    Typical:  ./apply_theme.py --accent 4166F5 --sddm --limine

Template syntax:  {{name}}   {{name|filter}}   {{name|filter:arg}}

    hex (default)  e2201f
    hash           #e2201f
    hasha:88       #e2201f88            RRGGBBAA, for rofi / CSS
    rgb            rgb(e2201f)          hyprland / hyprlock
    rgba:aa        rgba(e2201faa)       hyprland / hyprlock, alpha is 2 hex digits, default ff
    css:0.35       rgba(226, 32, 31, 0.35)
    ansi           226;32;31            for escape sequences like ESC[38;2;...m

To write a literal double-brace pair in a template, put a backslash in front of it.

Palette: background / foreground / accent are the inputs. muted, idle, surface,
border, border_inactive and shadow are derived from them unless palette.json
defines a key with the same name (that pins it). Keys starting with "_" are ignored.

Environment overrides:
    GEN_WALLPAPER          path to gen_wallpaper.py
    STARSHIP_CONFIG        starship output path
    SDDM_THEME_DIR         SDDM theme directory (default /usr/share/sddm/themes/nothing)
    LIMINE_CONF            limine.conf location (default /boot/EFI/BOOT/limine.conf)
    LIMINE_WALLPAPER       boot wallpaper location (default /boot/wallpaper.png)
    LIMINE_WALLPAPER_SIZE  WxH of the boot wallpaper (default 1920x1080)
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent
PALETTE_FILE = SCRIPT_DIR / "palette.json"
STATE_FILE = SCRIPT_DIR / "state.json"  # saved --accent override; add to .gitignore
TEMPLATE_DIR = SCRIPT_DIR / "templates"

MONITOR = "eDP-1"
WALLPAPER_OUT = HOME / ".wallpapers/Nothing1.png"
WALLPAPER_SCRIPT = Path(
    os.environ.get("GEN_WALLPAPER", HOME / "github/gen-wallpaper/gen_wallpaper.py")
)

# name -> (template filename, output path)
TARGETS = {
    "waybar": ("waybar-colors.css.tmpl", HOME / ".config/waybar/colors.css"),
    "eww": ("eww-colors.scss.tmpl", HOME / ".config/eww/colors.scss"),
    "hyprland": ("hypr-colors.lua.tmpl", HOME / ".config/hypr/colors.lua"),
    "hyprlock": ("hyprlock-colors.conf.tmpl", HOME / ".config/hypr/hyprlock-colors.conf"),
    "kitty": ("kitty-colors.conf.tmpl", HOME / ".config/kitty/colors.conf"),
    # Whole-file templates: the rendered file IS the app's config, so hand-edit
    # the template, never the output. Neither app has a reload step (rofi and
    # fastfetch read their config fresh on every launch).
    "rofi": ("rofi-nothing.rasi.tmpl", HOME / ".config/rofi/nothing.rasi"),
    "fastfetch": ("fastfetch.jsonc.tmpl", HOME / ".config/fastfetch/config.jsonc"),
    # Starship reads $STARSHIP_CONFIG, else ~/.config/starship.toml. Not the
    # ~/.config/starship/ directory your README symlinks.
    "starship": (
        "starship.toml.tmpl",
        Path(os.environ.get("STARSHIP_CONFIG", HOME / ".config/starship.toml")),
    ),
}

# Need root to install, so they are only ever staged next to this script and
# then copied with sudo by install_sddm() / install_limine().
# The key doubles as the CLI flag name (--sddm, --limine).
STAGED = {
    "sddm": ("sddm-theme.conf.tmpl", SCRIPT_DIR / "sddm-theme.conf.rendered"),
    "limine": ("limine-theme.conf.tmpl", SCRIPT_DIR / "limine-theme.conf.rendered"),
}

# --- SDDM -------------------------------------------------------------------

SDDM_THEME_CONF = Path(
    os.environ.get("SDDM_THEME_DIR", "/usr/share/sddm/themes/nothing")
) / "theme.conf.user"

# --- Limine -----------------------------------------------------------------

LIMINE_CONF = Path(os.environ.get("LIMINE_CONF", "/boot/EFI/BOOT/limine.conf"))
# boot():/wallpaper.png in limine.conf resolves to this file on your ESP.
LIMINE_WALLPAPER = Path(os.environ.get("LIMINE_WALLPAPER", "/boot/wallpaper.png"))
# Generated locally first, then copied to the ESP. Add to .gitignore.
LIMINE_WALLPAPER_SRC = SCRIPT_DIR / "limine-wallpaper.png"
# Set this to your panel's resolution (or the resolution Limine boots in).
LIMINE_WALLPAPER_SIZE = os.environ.get("LIMINE_WALLPAPER_SIZE", "1920x1080")
# Only the text between these markers is ever rewritten; boot entries are untouched.
LIMINE_BLOCK_RE = re.compile(
    r"^# BEGIN theme-switcher\n.*?^# END theme-switcher\n?", re.M | re.S
)


class ThemeError(Exception):
    pass


# --- color helpers ----------------------------------------------------------

def norm(value):
    h = str(value).strip().lstrip("#").lower()
    if not re.fullmatch(r"[0-9a-f]{6}", h):
        raise ThemeError(f"not a 6-digit hex color: {value!r}")
    return h


def to_rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    """Move color a the fraction t of the way toward b (t=0 -> a, t=1 -> b)."""
    return "".join(
        f"{round(x + (y - x) * t):02x}" for x, y in zip(to_rgb(a), to_rgb(b))
    )


def _toward_fg(t):
    return lambda p: mix(p["background"], p["foreground"], t)


# Tuned so the defaults land near the values you had hardcoded.
DERIVED = {
    "muted": _toward_fg(0.55),            # your README's "foreground at 0.55"
    "idle": _toward_fg(0.14),
    "surface": _toward_fg(0.03),
    "border": _toward_fg(0.07),
    "border_inactive": _toward_fg(0.27),
    "shadow": lambda p: mix(p["background"], "000000", 0.05),
}


def load_palette(cli_accent, use_state=True):
    raw = json.loads(PALETTE_FILE.read_text())
    if use_state and STATE_FILE.exists():
        raw.update(json.loads(STATE_FILE.read_text()))
    if cli_accent:
        raw["accent"] = cli_accent

    pal = {}
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        try:
            pal[key] = norm(value)
        except ThemeError as e:
            raise ThemeError(f"palette key {key!r}: {e}")

    for key in ("background", "foreground", "accent"):
        if key not in pal:
            raise ThemeError(f"palette is missing required key {key!r}")
    for name, fn in DERIVED.items():
        pal.setdefault(name, fn(pal))  # an explicit palette.json value wins
    return pal


# --- templating -------------------------------------------------------------

def _alpha(arg):
    a = (arg or "ff").lower()
    if not re.fullmatch(r"[0-9a-f]{2}", a):
        raise ThemeError(f"alpha must be 2 hex digits, got {arg!r}")
    return a


def _css_alpha(arg):
    try:
        return f"{float(arg if arg is not None else 1):g}"
    except ValueError:
        raise ThemeError(f"css alpha must be a number, got {arg!r}")


FILTERS = {
    "hex": lambda h, a: h,
    "hash": lambda h, a: f"#{h}",
    "hasha": lambda h, a: f"#{h}{_alpha(a)}",
    "rgb": lambda h, a: f"rgb({h})",
    "rgba": lambda h, a: f"rgba({h}{_alpha(a)})",
    "css": lambda h, a: "rgba({}, {}, {}, {})".format(*to_rgb(h), _css_alpha(a)),
    "ansi": lambda h, a: ";".join(str(v) for v in to_rgb(h)),
}

TOKEN_RE = re.compile(
    r"(?<!\\)\{\{\s*(\w+)\s*(?:\|\s*(\w+)\s*(?::\s*([\w.]+)\s*)?)?\}\}"
)


def render(text, pal, source):
    def repl(m):
        key, filt, arg = m.group(1), m.group(2) or "hex", m.group(3)
        if key not in pal:
            raise ThemeError(f"{source}: unknown token {key!r}")
        if filt not in FILTERS:
            raise ThemeError(f"{source}: unknown filter {filt!r} on {key!r}")
        try:
            return FILTERS[filt](pal[key], arg)
        except ThemeError as e:
            raise ThemeError(f"{source}: {key}|{filt}: {e}")

    return TOKEN_RE.sub(repl, text).replace("\\{{", "{{")


def build(pal, targets):
    """Phase 1: render everything in memory. Collect every error, write nothing."""
    outputs, errors = {}, []
    for name, (tmpl, out) in targets.items():
        path = TEMPLATE_DIR / tmpl
        if not path.exists():
            errors.append(f"{name}: missing template {path}")
            continue
        try:
            outputs[name] = (out, render(path.read_text(), pal, tmpl))
        except ThemeError as e:
            errors.append(str(e))
    return outputs, errors


def write_atomic(path, content):
    real = path.resolve()  # follow symlinks: replace the repo file, not the link
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, real)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- wallpaper + reloads ----------------------------------------------------

def run(cmd, label):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        print(f"  [skip] {label}: {cmd[0]} not found")
        return
    except subprocess.TimeoutExpired:
        print(f"  [warn] {label}: timed out")
        return
    if cmd[0] == "pkill" and r.returncode == 1:
        print(f"  [skip] {label}: not running")
    elif r.returncode:
        detail = r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}"
        print(f"  [warn] {label}: {detail}")
    else:
        print(f"  [ok]   {label}")


def regen_wallpaper(pal):
    """Returns the versioned image path (what hyprpaper must be given), or None."""
    if not WALLPAPER_SCRIPT.exists():
        print(f"  [skip] wallpaper: {WALLPAPER_SCRIPT} not found (set GEN_WALLPAPER)")
        return None
    WALLPAPER_OUT.parent.mkdir(parents=True, exist_ok=True)
    # --versioned: writes Nothing1-<hash>.png, repoints the stable Nothing1.png
    # symlink (rofi + hyprpaper.conf keep using that name) and prunes old versions.
    cmd = [
        sys.executable, str(WALLPAPER_SCRIPT),
        "--accent", pal["accent"],
        "--bg", pal["background"],
        "--base", pal["foreground"],
        "-o", str(WALLPAPER_OUT),
        "--versioned",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("  [warn] wallpaper: timed out")
        return None
    lines = r.stdout.strip().splitlines()
    if r.returncode or not lines:
        print(f"  [warn] wallpaper: {r.stderr.strip() or r.returncode}")
        return None
    path = Path(lines[-1])
    print(f"  [ok]   wallpaper -> {path}")
    return path


def reload_eww():
    run(["eww", "reload"], "eww")


def reload_hyprland():
    # UNVERIFIED: whether this re-reads a require()d colors.lua or serves a
    # cached module. Test by changing the accent and checking the active border.
    run(["hyprctl", "reload"], "hyprland")


def reload_kitty():
    colors = str(TARGETS["kitty"][1])
    # Outside a kitty window this needs `allow_remote_control socket-only` and
    # `listen_on unix:/tmp/kitty` in kitty.conf; kitty appends its pid to the path.
    socks = [] if os.environ.get("KITTY_LISTEN_ON") else sorted(Path("/tmp").glob("kitty-*"))
    cmds = [
        ["kitty", "@", "--to", f"unix:{s}", "set-colors", "--all", colors] for s in socks
    ] or [["kitty", "@", "set-colors", "--all", colors]]
    for c in cmds:
        run(c, "kitty")


def reload_hyprpaper(path):
    # hyprpaper ignores a changed file at an unchanged path, so hand it the
    # versioned path, not the stable Nothing1.png symlink.
    run(["hyprctl", "hyprpaper", "wallpaper", f"{MONITOR},{path},cover"], "hyprpaper")


# --- root-only installs (SDDM, Limine) --------------------------------------

def sudo_run(cmd):
    """Run `sudo <cmd>` attached to the terminal so the password prompt works.
    No capture_output and no timeout on purpose."""
    try:
        return subprocess.run(["sudo", *cmd]).returncode == 0
    except FileNotFoundError:
        print("  [warn] sudo not found")
        return False


def read_maybe_root(path):
    """Read a file as the user, falling back to `sudo cat` (the ESP may be root-only)."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return None
    except PermissionError:
        r = subprocess.run(["sudo", "cat", str(path)], stdout=subprocess.PIPE, text=True)
        return r.stdout if r.returncode == 0 else None


def install_sddm():
    src = STAGED["sddm"][1]
    dest = SDDM_THEME_CONF
    if not dest.parent.is_dir():
        print(f"  [warn] sddm: {dest.parent} does not exist (set SDDM_THEME_DIR)")
        return
    if dest.exists() and dest.read_text() == src.read_text():
        print("  [same]  sddm (already installed)")
        return
    print(f"\nSDDM: installing to {dest} (sudo may ask for your password)")
    if sudo_run(["install", "-m", "644", str(src), str(dest)]):
        print("  [ok]   sddm (takes effect the next time the greeter starts)")
    else:
        print("  [warn] sddm: install failed")


def install_limine():
    """Replace only the '# BEGIN/END theme-switcher' block inside limine.conf."""
    block = STAGED["limine"][1].read_text().rstrip("\n") + "\n"
    current = read_maybe_root(LIMINE_CONF)
    if current is None:
        print(f"  [warn] limine: cannot read {LIMINE_CONF} (set LIMINE_CONF)")
        return
    if not LIMINE_BLOCK_RE.search(current):
        print(
            f"  [warn] limine: no '# BEGIN/END theme-switcher' markers in "
            f"{LIMINE_CONF}; nothing changed"
        )
        return
    new = LIMINE_BLOCK_RE.sub(lambda m: block, current, count=1)
    if new == current:
        print("  [same]  limine (already installed)")
        return
    print(f"\nLimine: updating {LIMINE_CONF} (sudo may ask for your password)")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "limine.conf"
        tmp.write_text(new)
        if not sudo_run(["cp", "-f", str(LIMINE_CONF), f"{LIMINE_CONF}.bak"]):
            print("  [warn] limine: backup failed, aborting")
            return
        # cp (not install -m): chmod can fail on FAT filesystems like an ESP.
        if sudo_run(["cp", str(tmp), str(LIMINE_CONF)]):
            print(f"  [ok]   limine (backup: {LIMINE_CONF}.bak)")
        else:
            print("  [warn] limine: copy failed")


def update_limine_wallpaper(pal):
    """Generate a fresh wallpaper with the current palette and copy it to the ESP."""
    if not WALLPAPER_SCRIPT.exists():
        print(f"  [skip] limine wallpaper: {WALLPAPER_SCRIPT} not found (set GEN_WALLPAPER)")
        return
    cmd = [
        sys.executable, str(WALLPAPER_SCRIPT),
        "--accent", pal["accent"],
        "--bg", pal["background"],
        "--base", pal["foreground"],
        "--size", LIMINE_WALLPAPER_SIZE,
        "-o", str(LIMINE_WALLPAPER_SRC),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("  [warn] limine wallpaper: generation timed out")
        return
    if r.returncode:
        print(f"  [warn] limine wallpaper: {r.stderr.strip() or r.returncode}")
        return

    # The generator is deterministic, so identical bytes means nothing to do.
    # cmp runs under sudo because the ESP may be root-only; exit 0 = identical.
    if subprocess.run(
        ["sudo", "cmp", "-s", str(LIMINE_WALLPAPER_SRC), str(LIMINE_WALLPAPER)]
    ).returncode == 0:
        print("  [same]  limine wallpaper (already installed)")
        return
    if sudo_run(["cp", str(LIMINE_WALLPAPER_SRC), str(LIMINE_WALLPAPER)]):
        print(f"  [ok]   limine wallpaper -> {LIMINE_WALLPAPER}")
    else:
        print("  [warn] limine wallpaper: copy failed")


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--accent", metavar="HEX", help="override + save the accent color")
    ap.add_argument("--reset", action="store_true", help="drop the saved accent override")
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    ap.add_argument("--diff", action="store_true", help="dry run that also prints a diff per changed file")
    ap.add_argument("--force", action="store_true", help="regen wallpaper + reload even if unchanged")
    ap.add_argument("--no-reload", action="store_true")
    ap.add_argument("--no-wallpaper", action="store_true")
    ap.add_argument("--sddm", action="store_true", help="also render + install the SDDM theme (sudo)")
    ap.add_argument("--limine", action="store_true", help="also update the Limine theme + boot wallpaper (sudo)")
    args = ap.parse_args()

    args.dry_run = args.dry_run or args.diff
    if args.accent and args.reset:
        ap.error("--accent and --reset are mutually exclusive")
    accent = None
    if args.accent:
        try:
            accent = norm(args.accent)
        except ThemeError as e:
            ap.error(str(e))

    try:
        pal = load_palette(accent, use_state=not args.reset)
    except (ThemeError, OSError, json.JSONDecodeError) as e:
        sys.exit(f"palette error: {e}")

    targets = dict(TARGETS)
    targets.update({k: v for k, v in STAGED.items() if getattr(args, k)})

    outputs, errors = build(pal, targets)
    if errors:
        print("Nothing written. Fix these first:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    print(f"accent #{pal['accent']}" + ("  (dry run)" if args.dry_run else ""))
    live_changed = False
    for name, (out, content) in outputs.items():
        real = out.resolve()
        old = real.read_text() if real.exists() else ""
        if real.exists() and old == content:
            print(f"  [same]  {name}")
            continue
        if name in TARGETS:
            live_changed = True
        if args.dry_run:
            print(f"  [would write] {name} -> {out}")
            if args.diff:
                sys.stdout.writelines(
                    "      " + line if line.endswith("\n") else "      " + line + "\n"
                    for line in difflib.unified_diff(
                        old.splitlines(), content.splitlines(),
                        "current", "rendered", lineterm="", n=0)
                )
            continue
        write_atomic(out, content)
        print(f"  [ok]    {name} -> {out}")

    if args.dry_run:
        return

    if accent:
        STATE_FILE.write_text(json.dumps({"accent": accent}, indent=2) + "\n")
    if args.reset:
        STATE_FILE.unlink(missing_ok=True)

    # Root-only installs. These sit before the "nothing changed" early return
    # on purpose: they must run whenever their flag is passed.
    if args.sddm:
        install_sddm()
    if args.limine:
        install_limine()
        if not args.no_wallpaper:
            update_limine_wallpaper(pal)

    if not (live_changed or args.force):
        print("\nNothing changed; skipping wallpaper and reloads.")
        return

    wallpaper_path = None
    if not args.no_wallpaper:
        print("\nWallpaper:")
        wallpaper_path = regen_wallpaper(pal)

    if not args.no_reload:
        print("\nReloading:")
        reload_eww()
        reload_hyprland()
        reload_kitty()
        # waybar needs no call: "reload_style_on_change": true in config.jsonc
        if wallpaper_path:
            reload_hyprpaper(wallpaper_path)


if __name__ == "__main__":
    main()
