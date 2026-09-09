#!/usr/bin/env python3
"""
apply_theme.py — regenerate per-app color files from one palette.json
and trigger whatever reload mechanism each app actually needs.

Layout expected next to this file:
    palette.json
    templates/*.tmpl

Usage:
    ./apply_theme.py                # render + reload everything
    ./apply_theme.py --no-reload    # only write the files
    ./apply_theme.py --sddm         # also print the SDDM restart step
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PALETTE_FILE = SCRIPT_DIR / "palette.json"
TEMPLATE_DIR = SCRIPT_DIR / "templates"

TOKEN_RE = re.compile(r"\{\{(\w+)\}\}")

# name -> (template filename, real output path)
# Adjust the output paths to match your actual dotfiles.
TARGETS = {
    "waybar": (
        "waybar-colors.css.tmpl",
        Path.home() / ".config/waybar/colors.css",
    ),
    "eww": (
        "eww-colors.scss.tmpl",
        Path.home() / ".config/eww/colors.scss",
    ),
    "hyprland": (
        "hypr-colors.lua.tmpl",
        Path.home() / ".config/hypr/colors.lua",
    ),
    "hyprlock": (
        "hyprlock-colors.conf.tmpl",
        Path.home() / ".config/hypr/hyprlock-colors.conf",
    ),
    "kitty": (
        "kitty-colors.conf.tmpl",
        Path.home() / ".config/kitty/colors.conf",
    ),
    "starship": (
        "starship.toml.tmpl",
        Path.home() / ".config/starship.toml",
    ),
}

# Rendered separately: needs root to install, so we only write it to a
# staging path and print the command instead of touching /usr/share.
SDDM_TEMPLATE = "sddm-theme.conf.tmpl"
SDDM_STAGING = SCRIPT_DIR / "sddm-theme.conf.rendered"


def load_palette() -> dict:
    with open(PALETTE_FILE) as f:
        return json.load(f)


def render(text: str, palette: dict, is_hyprland: bool = False) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key not in palette:
            raise KeyError(f"palette.json is missing key: {key}")
        
        val = palette[key]
        # Si la cible est Hyprland, on formate automatiquement en rgba(...)
        if is_hyprland:
            return hex_to_rgba(val)
        
        return val

    return TOKEN_RE.sub(repl, text)


def render_all(palette: dict) -> None:
    for name, (template_name, out_path) in TARGETS.items():
        tmpl_path = TEMPLATE_DIR / template_name
        if not tmpl_path.exists():
            print(f"  [skip] {name}: no template at {tmpl_path}")
            continue
        
        # On passe True si le template est destiné à Hyprland
        is_hyprland = "hyprland" in name
        rendered = render(tmpl_path.read_text(), palette, is_hyprland=is_hyprland)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered)
        print(f"  [ok]   {name} -> {out_path}")


def render_sddm(palette: dict) -> None:
    tmpl_path = TEMPLATE_DIR / SDDM_TEMPLATE
    if not tmpl_path.exists():
        return
    rendered = render(tmpl_path.read_text(), palette)
    SDDM_STAGING.write_text(rendered)
    print(f"  [ok]   sddm -> staged at {SDDM_STAGING} (not installed)")


def hex_to_rgba(hex_color: str, alpha: str = "ff") -> str:
    # Nettoie le '#' au cas où il y en a un, puis génère rgba(HEXalpha)
    clean_hex = str(hex_color).lstrip('#')
    return f"rgba({clean_hex}{alpha})"

# --- reload mechanisms -----------------------------------------------------
# Waybar and eww watch their own config files, so nothing has to be sent to
# them — they're listed here only as a place to add a forced-restart
# fallback later if you ever need one (see the comments below).

def reload_waybar() -> None:
    pass  # requires "reload_style_on_change": true in config.jsonc
    # Fallback if that ever misbehaves (e.g. custom -s path + @import):
    # subprocess.run(["pkill", "-x", "waybar"], check=False)
    # subprocess.Popen(["waybar"], start_new_session=True)


def reload_eww() -> None:
    subprocess.run(["eww", "reload"], check=False)


def reload_hyprland() -> None:
    # Hyprland reloads sourced files on save already; this just makes
    # sure it happens immediately rather than on Hyprland's own timing.
    subprocess.run(["hyprctl", "reload"], check=False)


def reload_kitty() -> None:
    _, colors_path = TARGETS["kitty"]
    try:
        subprocess.run(
            ["kitty", "@", "set-colors", "--all", "-a", str(colors_path)],
            check=True,
            timeout=2,
            capture_output=True,
        )
        print("  [ok]   kitty: pushed live via remote control")
    except FileNotFoundError:
        print("  [skip] kitty: binary not found")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print(
            "  [warn] kitty: remote control unavailable "
            "(check 'allow_remote_control' in kitty.conf) — "
            "new windows will still pick up the theme"
        )


def print_sddm_instructions() -> None:
    print(
        "\nSDDM: copy the staged file into your theme, then restart the "
        "greeter manually (needs root, and is worth testing once before "
        "you fully trust it in a script):\n"
        f"  sudo cp {SDDM_STAGING} /usr/share/sddm/themes/<your-theme>/theme.conf.user\n"
        "  sudo systemctl restart sddm"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-reload", action="store_true", help="only write files, skip reload calls"
    )
    parser.add_argument(
        "--sddm", action="store_true", help="also render + print the SDDM step"
    )
    args = parser.parse_args()

    if not PALETTE_FILE.exists():
        sys.exit(f"palette.json not found at {PALETTE_FILE}")

    palette = load_palette()

    print("Rendering:")
    render_all(palette)
    if args.sddm:
        render_sddm(palette)

    if not args.no_reload:
        print("\nReloading:")
        reload_eww()
        reload_hyprland()
        reload_kitty()
        reload_waybar()  # no-op by design, see comment above

    if args.sddm:
        print_sddm_instructions()


if __name__ == "__main__":
    main()
