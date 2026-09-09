# theme-switcher (scaffold)

One palette in, per-app color files out, each app's own reload mechanism
triggered automatically where one exists.

```
theme-switcher/
├── palette.json          # the single source of truth
├── apply_theme.py         # renders templates/*, then reloads apps
└── templates/
    ├── waybar-colors.css.tmpl
    ├── eww-colors.scss.tmpl
    ├── hypr-colors.conf.tmpl
    ├── hyprlock-colors.conf.tmpl
    ├── kitty-colors.conf.tmpl
    ├── starship.toml.tmpl
    └── sddm-theme.conf.tmpl
```

## Before running it

1. Update the output paths in `TARGETS` inside `apply_theme.py` if your
   dotfiles don't live at the default `~/.config/<app>/...` locations.
2. In each real app config, point it at the generated file:
   - `waybar/style.css` → `@import url("colors.css");`
   - `eww/eww.scss` → `@import 'colors.scss';`
   - `hypr/hyprland.conf` → `source = ~/.config/hypr/colors.conf`
   - `hypr/hyprlock.conf` → `source = ~/.config/hypr/hyprlock-colors.conf`
   - `kitty/kitty.conf` → `include colors.conf` and `allow_remote_control yes`
   - `starship.toml` — this template *is* the whole file; move your real
     config into `templates/starship.toml.tmpl` and drop `{{tokens}}`
     into the `style =` strings.
3. Enable `"reload_style_on_change": true` in `waybar/config.jsonc`.

## Running it

```
chmod +x apply_theme.py
./apply_theme.py            # render + reload waybar/eww/hyprland/kitty
./apply_theme.py --sddm     # also stage the sddm theme.conf + print the restart command
./apply_theme.py --no-reload
```

## Why each app is handled the way it is

| App | Needs a reload call? | Why |
|---|---|---|
| Waybar | No | watches its style file itself (`reload_style_on_change`) |
| eww | Script calls `eww reload` | belt-and-suspenders; it also self-watches |
| Hyprland | Script calls `hyprctl reload` | reloads on save anyway; this just forces timing |
| Hyprlock | No | not a daemon — reads its config fresh at next lock |
| Kitty | Script calls `kitty @ set-colors` | no self-watcher; needs `allow_remote_control` |
| Starship | No | reads its TOML fresh on every prompt render |
| SDDM | Manual, root, printed not run | separate systemd service, greeter reads once at start |

## One gotcha

The renderer matches literal `{{word}}` anywhere in a template file,
including inside comments — it doesn't know the difference between a
real placeholder and you writing about placeholders in prose. If you add
comments to a template, don't wrap example token names in double curly
braces or the script will try to substitute them and fail with a
missing-key error.

## Next steps

This is a scaffold, not your real theme — the template files above only
carry a handful of placeholder colors as a proof of concept. Fill in the
`{{tokens}}` your actual dotfiles reference, and extend `palette.json`
with anything else you need (multiple accent colors, alpha values, etc.).
Once this is solid, that's the point to build the interface on top of it.
