"""Menu Icons: replaces the bottom-right HUD row (Menu/Minimap/Squad/Galaxy +
the Menu dropup's items) with a RuneScape-style icon bar: a few always-visible
buttons plus category buttons that drop up into a strip of more icons.

Layout (left to right):  Universe Map | Squad | Minimap | Character | Social |
Economy | Options.  Character, Social and Economy are categories; clicking one
drops a strip of icons up above it (click again, click elsewhere or press Esc to
close):
  * Character: Stats, Missions, Run Summary, Mechlings
  * Social:    Friends, Coalition, Mail
  * Economy:   Global Market, Attendance Rewards, Shop (only while the server
               enables it)
A category shows a red dot while anything inside it needs attention.

Approach:
  * Patches SolarSystemWindow._draw_hud_launchers (render_mixin.py) to draw the
    bar instead of the original four text buttons.
  * Still publishes self._galaxy_map_btn_rect / _squad_btn_rect /
    _minimap_btn_rect exactly as the original did, so the game's own,
    untouched click-handling code for those three keeps working unchanged.
  * self._menu_btn_rect is set to None every frame, so the original
    "toggle _menu_open" click handler never fires and the now-orphaned
    dropup (_draw_menu_popup) never draws -- no need to patch it at all.
  * The dropup strip and tooltips are drawn from the client.draw hook, which
    runs after every ordinary window and popover, so they sit above windows
    (the launcher row itself stays in its retained layer under them, like
    the game's own).
  * Category buttons and the dropup strip are mod-drawn, so their clicks are
    owned here via the client.event hook, which *consumes* them (returns True)
    so a strip floating over a game window can't also click that window.
  * Right-clicking the Universe Map (galaxy) icon copies the current system
    name to the clipboard and pops a small self-drawn toast confirming it.

Game entries covered: Stats, Friends, Missions, Run Summary, Coalition, Mail
(mail_gui.toggle), Attendance, Global Market, Mechlings
(mechlings_client.toggle), Shop. Mail/Mechlings/Run Summary/Shop appear only
when the running game has them.
"""
import importlib
import logging
import os
import sys
import time

_log = logging.getLogger("star_empire_mod.menu-icons")
_warned = set()


def _warn_once(key, msg):
    if key not in _warned:
        _warned.add(key)
        _log.warning("menu-icons: %s", msg)

_ICON_FILES = {
    "galaxy": "globe.png",
    "squad": "user_plus.png",
    "minimap": "map.png",
    "stats": "bar_chart_2.png",
    "friends": "user_check.png",
    "missions": "flag.png",
    "runs": "clipboard.png",
    "coalition": "shield.png",
    "mail": "mail.png",
    "attendance": "calendar.png",
    "market": "shopping_cart.png",
    "mechlings": "robot.png",
    "options": "settings.png",
    "cat:character": "user.png",
    "cat:social": "users.png",
    "cat:economy": "dollar_sign.png",
}

# (category key, title, member keys in display order)
_CATEGORIES = (
    ("character", "Character", ("stats", "missions", "runs", "mechlings")),
    ("social", "Social", ("friends", "coalition", "mail")),
    ("economy", "Economy", ("market", "attendance", "shop")),
)

# Direct buttons this mod owns the click for (galaxy/squad/minimap keep native
# handling; dropup members and category buttons are handled separately).
_MOD_OWNED_KEYS = ("options",)

_icons = {}
_item_rects = {}          # direct row buttons, incl. "cat:<key>" categories
_dropup_rects = {}        # member key -> rect, for the open category only
_dropup_strip = [None]    # backdrop rect of the open dropup
_open_category = [None]   # key of the category whose dropup is open
_install_path = [None]
_original_draw_hud_launchers = None
_patched_class = None

# Current system name, refreshed every draw frame so the right-click handler
# (which fires from client.event, not from inside the draw pass) always has
# an up-to-date value to copy without needing its own lock/state lookup.
_current_system_name = [None]

_TOAST_FADE = 0.35  # seconds of fade-out at the tail end of _TOAST_LIFE
_TOAST_LIFE = 1.8   # total seconds a toast stays visible (incl. fade)
_toast = {"text": None, "shown_at": 0.0}


def _load_icons(pygame, install_path):
    icons_dir = os.path.join(str(install_path), "mod", "menu_icons", "icons")
    for key, filename in _ICON_FILES.items():
        path = os.path.join(icons_dir, filename)
        try:
            _icons[key] = pygame.image.load(path).convert_alpha()
        except Exception:
            _icons[key] = None


def _open_attendance(host):
    host._attendance_gui.show()
    if host._galaxy_map_open:
        host._close_galaxy_map()


def _open_market(host):
    host._open_market()
    if host._galaxy_map_open:
        host._close_galaxy_map()


_pending_open_options = [False]
_pending_mission_target = [None]
_pending_runs_target = [None]
_pending_open_shop = [False]


def _game_module(name):
    """A loose game module (run_summary_gui, premium_shop, mail_gui,
    mechlings_client), or None. Client.py
    imports both itself, so they are normally already in sys.modules."""
    module = sys.modules.get(name)
    if module is not None:
        return module
    try:
        return importlib.import_module(name)
    except Exception:
        _warn_once("import:" + name, f"game module {name} unavailable")
        return None


def _runs_state(host):
    """(open, attention, meta) of the Run Summary window, or None when this
    game build has none."""
    state = getattr(host, "_run_summary", None)
    if state is None:
        return None
    try:
        meta, new = state.menu_meta()
    except Exception:
        meta, new = "", False
    return bool(getattr(state, "open", False)), bool(new), str(meta or "")


def _shop_state(host):
    """(enabled, open) of the premium shop, or (False, False)."""
    shop = getattr(host, "_premium_shop", None)
    if shop is None:
        return False, False
    return (bool(getattr(shop, "enabled", False)),
            bool(getattr(shop, "open", False)))


def _menu_window_state(host, attr):
    """(open, attention, meta) of a Menu window that has a menu_meta() (Mail,
    Mechlings), or None when this game build doesn't have it."""
    state = getattr(host, attr, None)
    if state is None:
        return None
    try:
        meta, attention = state.menu_meta()
    except Exception:
        meta, attention = "", False
    return bool(getattr(state, "open", False)), bool(attention), str(meta or "")


def _toggle_menu_window(host, module_name):
    """What the Menu's MAIL / MECHLINGS rows call: <module>.toggle(host), then
    close the galaxy map like the native click handler does."""
    module = _game_module(module_name)
    if module is None:
        return
    module.toggle(host)
    if host._galaxy_map_open:
        host._close_galaxy_map()


def _open_mail(host):
    _toggle_menu_window(host, "mail_gui")


def _open_mechlings(host):
    _toggle_menu_window(host, "mechlings_client")


def _open_runs(host):
    # Deferred like Missions (see _open_missions): applied on the next
    # client.frame.begin, after native handling of this click has run.
    state = _runs_state(host)
    if state is not None:
        _pending_runs_target[0] = not state[0]


def _apply_pending_runs(host):
    target = _pending_runs_target[0]
    if target is None:
        return
    _pending_runs_target[0] = None
    state = _runs_state(host)
    if state is None or state[0] == target:
        return
    module = _game_module("run_summary_gui")
    if module is None:
        return
    try:
        module.toggle(host)       # what the Menu's RUN SUMMARY row calls
    except Exception:
        _log.exception("menu-icons: toggling Run Summary failed")


def _open_shop(host):
    _pending_open_shop[0] = True


def _apply_pending_shop(host):
    if not _pending_open_shop[0]:
        return
    _pending_open_shop[0] = False
    module = _game_module("premium_shop")
    if module is None:
        return
    try:
        # Mirrors the Menu's SHOP row (Client.py menu popup click handler).
        host._menu_open = False
        module.open_shop(host)
        if host._galaxy_map_open:
            host._close_galaxy_map()
    except Exception:
        _log.exception("menu-icons: opening the Shop failed")


def _open_missions(host):
    # Deferred rather than calling host._toggle_mission_log() straight from
    # this click handler: the missions icon lives outside the set of rects
    # Client.py's own mouse handler still treats as "a HUD launcher, consumed
    # earlier" (only galaxy/squad/minimap keep that native carve-out now --
    # see the module docstring). So while the panel is open, the same click
    # that reaches us here also reaches Client.py's own "click outside
    # Mission Log panel closes it" fallback, and whichever of the two runs
    # last wins -- the toggle and the native close fight over the same
    # event, and the panel can never settle open. Capturing the intended
    # end state now and applying it on the next client.frame.begin (after
    # all native handling for this event has already run its course) lets
    # us land on the state we actually meant regardless of hook order.
    _pending_mission_target[0] = not host._mission_log_open


def _apply_pending_missions(host):
    target = _pending_mission_target[0]
    if target is None:
        return
    _pending_mission_target[0] = None
    if host._mission_log_open == target:
        return
    host._menu_open = False
    host._mission_log_open = target
    if target:
        host._send_fn("MY_MISSIONS")
    else:
        host._mission_log_search_focused = False


def _open_options(host):
    # Deferred rather than setting self._esc_menu_open directly: while the
    # esc menu is open, Client.py's own click handler (the block guarded by
    # "if self._esc_menu_open and _gui_route_allows('escape_menu')") closes
    # it again on any click that doesn't land on its own panel rect -- and
    # this button lives outside that panel. Setting the flag straight from
    # this click handler let that native "click outside closes it" check
    # see the very same click event and immediately undo it, so the button
    # would open the menu and have it vanish (or get stuck refusing to
    # reopen) depending on hook ordering. Applying it on the next
    # client.frame.begin instead means there's no pending click event left
    # for that native check to react to.
    _pending_open_options[0] = True


def _apply_pending_options(host):
    if not _pending_open_options[0]:
        return
    _pending_open_options[0] = False
    # Mirrors the Escape-key handler's own "open" branch (Client.py, the
    # K_ESCAPE block's final else): just raise the flag and clear any
    # stale slider-drag state so the panel opens cleanly.
    host._esc_menu_open = True
    host._opt_slider_dragging = False
    host._opt_sfx_slider_dragging = False


def _on_frame_begin(host, render_target):
    _apply_pending_options(host)
    _apply_pending_missions(host)
    _apply_pending_runs(host)
    _apply_pending_shop(host)


_ACTIONS = {
    "stats": lambda host: host._toggle_stats_panel(),
    "friends": lambda host: host._toggle_friends_panel(),
    "missions": _open_missions,
    "runs": _open_runs,
    "coalition": lambda host: host._toggle_coalition_panel(),
    "mail": _open_mail,
    "mechlings": _open_mechlings,
    "attendance": _open_attendance,
    "market": _open_market,
    "shop": _open_shop,
    "options": _open_options,
}


_ui = [None]   # the latest frame's layout/entries, shared with the overlay
_UI_STALE = 0.5  # seconds; the overlay skips a layout the launcher stopped drawing


def _draw_button(pygame, host, screen, key, rect, ui, hover_pos):
    active, attention, _title, _subtitle = ui["entries"][key]
    hover = rect.collidepoint(hover_pos)
    s = host._s
    fill = ((18, 58, 94) if active else
            (18, 42, 72) if hover else (11, 24, 43))
    border = (95, 170, 255) if (active or hover) else (56, 88, 138)
    pygame.draw.rect(screen, fill, rect, border_radius=s(4))
    pygame.draw.rect(screen, border, rect, 1, border_radius=s(4))

    icon = _icons.get(key)
    if key == "galaxy":
        icon_pad = s(10)
        if icon is not None:
            icon_rect = icon.get_rect(
                midleft=(rect.left + icon_pad, rect.centery))
            screen.blit(icon, icon_rect)
            text_left = icon_rect.right + s(6)
        else:
            text_left = rect.left + icon_pad
        name_font = host._instrument_font(11, bold=True)
        name_color = (228, 237, 250) if (active or hover) \
            else (185, 215, 250)
        name_label = host._cached_fitted_label(
            str(ui["system_name"]), name_color, name_font,
            rect.right - s(8) - text_left)
        screen.blit(name_label, (
            text_left, rect.centery - name_label.get_height() // 2))
    elif key == "shop":
        gem = _shop_icon()
        if gem is not None:
            screen.blit(gem, gem.get_rect(center=rect.center))
    elif icon is not None:
        screen.blit(icon, icon.get_rect(center=rect.center))

    if key.startswith("cat:"):
        # Small caret, bottom-right: up = will drop up, down = open.
        cx = rect.right - s(8)
        top = rect.bottom - s(9)
        bottom = rect.bottom - s(5)
        tip, base = (bottom, top) if active else (top, bottom)
        half = s(3)
        pygame.draw.polygon(
            screen,
            (149, 188, 235) if (active or hover) else (96, 132, 186),
            [(cx - half, base), (cx + half, base), (cx, tip)])

    if attention:
        dot = s(8)
        dot_rect = pygame.Rect(
            rect.right - dot - s(2), rect.top + s(2), dot, dot)
        pygame.draw.ellipse(screen, (255, 92, 72), dot_rect)
        pygame.draw.ellipse(screen, (8, 18, 33), dot_rect, 1)


def _draw_tooltip(pygame, host, screen, ui, hover_pos):
    entries, strip = ui["entries"], ui["strip"]
    hovered = None   # (key, rect, top edge the tooltip sits above)
    for key, rect in ui["dropup_rects"].items():
        if rect.collidepoint(hover_pos):
            hovered = (key, rect, strip.top)
    if hovered is None:
        for key in ui["row_keys"]:
            rect = ui["rects"][key]
            # An open category's own tooltip would sit under its strip.
            if rect.collidepoint(hover_pos) and not (
                    key.startswith("cat:") and entries[key][0]):
                hovered = (key, rect, rect.top)
    if hovered is None:
        return

    key, rect, anchor_top = hovered
    _active, _attention, title, subtitle = entries[key]
    s = host._s
    title_label = host._instrument_font(10, bold=True).render(
        title.upper(), True, (228, 237, 250))
    sub_label = None
    if subtitle:
        sub_label = host._instrument_font(9, bold=False).render(
            subtitle.upper(), True, (149, 188, 235))

    pad_t = s(6)
    line_gap = s(2)
    content_w = title_label.get_width()
    content_h = title_label.get_height()
    if sub_label is not None:
        content_w = max(content_w, sub_label.get_width())
        content_h += line_gap + sub_label.get_height()

    box = pygame.Rect(0, 0, content_w + pad_t * 2, content_h + pad_t * 2)
    box.midbottom = (rect.centerx, anchor_top - s(4))
    box.left = max(s(4), min(box.left, ui["win_w"] - box.width - s(4)))
    pygame.draw.rect(screen, (8, 18, 33), box, border_radius=s(3))
    pygame.draw.rect(screen, (56, 88, 138), box, 1, border_radius=s(3))
    screen.blit(title_label, (box.x + pad_t, box.y + pad_t))
    if sub_label is not None:
        screen.blit(sub_label, (
            box.x + pad_t,
            box.y + pad_t + title_label.get_height() + line_gap))


def _on_draw(host, pygame, screen, render_target):
    """client.draw runs after every ordinary window and popover, so the dropup
    strip and the tooltips live here to sit above them (the launcher row
    itself stays in its retained layer, like the game's own)."""
    ui = _ui[0]
    if (ui is None or _icons_failed[0]
            or getattr(host, "_disconnected", False)
            or time.monotonic() - ui["stamp"] > _UI_STALE):
        return
    try:
        hover_pos = pygame.mouse.get_pos()
        strip = ui["strip"]
        if strip is not None:
            s = host._s
            pygame.draw.rect(screen, (8, 18, 33), strip, border_radius=s(5))
            pygame.draw.rect(
                screen, (95, 170, 255), strip, 1, border_radius=s(5))
            for key, rect in ui["dropup_rects"].items():
                _draw_button(pygame, host, screen, key, rect, ui, hover_pos)
        _draw_tooltip(pygame, host, screen, ui, hover_pos)
    except Exception:
        _warn_once("overlay", "dropup overlay draw failed; see log")
        _log.exception("menu-icons: dropup overlay draw failed")


def _make_draw_hud_launchers(pygame):
    def _draw_hud_launchers_icons(self, ctx):
        win_w, win_h = ctx.win_w, ctx.win_h
        btn = self._s(40)
        galaxy_w = self._s(150)
        gap = self._s(4)
        pad = self._s(8)
        button_y = win_h - btn - pad
        widths = {"galaxy": galaxy_w}

        with self._lock:
            mission_ready = sum(
                1 for mission in self._my_missions_active
                if mission.get("ready_to_claim"))
            system_name = self._layout.get("system_name", "Galaxy Map")
        _current_system_name[0] = system_name
        friends_pending = int(self._friends_state.get("pending_count", 0) or 0)
        attendance_ready = self._attendance_gui.notification_count
        squad_has_members = bool(self._squad_state.get("members"))
        runs = _runs_state(self)
        mail = _menu_window_state(self, "_mail")
        mechlings = _menu_window_state(self, "_mechlings")
        shop_enabled, shop_open = _shop_state(self)

        # key -> (active, attention, tooltip title, tooltip subtitle or None)
        entries = {
            "galaxy": (bool(self._galaxy_map_open), False, "Universe Map", None),
            "squad": (bool(self._squad_open), squad_has_members, "Squad", None),
            "minimap": (bool(self._mm_open), False, "Minimap", None),
            "stats": (bool(self._stats_open), False, "Stats", "[L]"),
            "friends": (
                bool(self._friends_open), bool(friends_pending), "Friends",
                f"{friends_pending} PENDING" if friends_pending else None),
            "missions": (
                bool(self._mission_log_open), bool(mission_ready), "Missions",
                f"[J]  {mission_ready} READY" if mission_ready else "[J]"),
            "coalition": (bool(self._coalition_open), False, "Coalition", None),
            "attendance": (
                bool(self._attendance_gui.open), bool(attendance_ready),
                "Attendance Rewards",
                f"{attendance_ready} READY" if attendance_ready else None),
            "market": (bool(self._market_open), False, "Global Market", None),
            "options": (bool(self._esc_menu_open), False, "Options", None),
        }
        if runs is not None:
            entries["runs"] = (runs[0], runs[1], "Run Summary",
                               runs[2] or None)
        if mail is not None:
            entries["mail"] = (mail[0], mail[1], "Mail", mail[2] or None)
        if mechlings is not None:
            entries["mechlings"] = (
                mechlings[0], mechlings[1], "Mechlings", mechlings[2] or None)
        if shop_enabled:
            entries["shop"] = (shop_open, False, "Shop", None)

        # Categories keep only the members this game build / server has.
        categories = []
        for cat_key, cat_title, member_keys in _CATEGORIES:
            members = [k for k in member_keys if k in entries]
            if not members:
                continue
            categories.append((cat_key, members))
            entries["cat:" + cat_key] = (
                _open_category[0] == cat_key,
                any(entries[k][1] for k in members),
                cat_title,
                "  ·  ".join(entries[k][2] for k in members))
        if _open_category[0] not in {c[0] for c in categories}:
            _open_category[0] = None

        row_keys = (["galaxy", "squad", "minimap"]
                    + ["cat:" + c[0] for c in categories] + ["options"])
        total_w = (sum(widths.get(k, btn) for k in row_keys)
                   + (len(row_keys) - 1) * gap)
        cursor_x = win_w - pad - total_w
        rects = {}
        for key in row_keys:
            w = widths.get(key, btn)
            rects[key] = pygame.Rect(cursor_x, button_y, w, btn)
            cursor_x += w + gap

        # Dropup strip for the open category: centred above its button and
        # clamped on screen. Geometry is published every frame (hit-testing).
        strip = None
        dropup_rects = {}
        open_members = next(
            (m for c, m in categories if c == _open_category[0]), None)
        if open_members is not None:
            strip_pad = self._s(4)
            strip_w = (len(open_members) * btn
                       + (len(open_members) - 1) * gap + strip_pad * 2)
            strip = pygame.Rect(0, 0, strip_w, btn + strip_pad * 2)
            anchor = rects["cat:" + _open_category[0]]
            strip.midbottom = (anchor.centerx, anchor.top - self._s(4))
            strip.left = max(
                self._s(4), min(strip.left, win_w - strip_w - self._s(4)))
            member_x = strip.left + strip_pad
            for key in open_members:
                dropup_rects[key] = pygame.Rect(
                    member_x, strip.top + strip_pad, btn, btn)
                member_x += btn + gap

        # Native click handling for these three is untouched -- just keep
        # publishing the rects it already expects.
        self._galaxy_map_btn_rect = rects["galaxy"]
        self._squad_btn_rect = rects["squad"]
        self._minimap_btn_rect = rects["minimap"]
        # No standalone Menu button anymore, so its toggle handler never
        # fires and the dropup (_draw_menu_popup) never opens.
        self._menu_btn_rect = None

        _item_rects.clear()
        _item_rects.update(rects)
        _dropup_rects.clear()
        _dropup_rects.update(dropup_rects)
        _dropup_strip[0] = strip

        ui = {
            "entries": entries, "row_keys": row_keys, "rects": rects,
            "dropup_rects": dict(dropup_rects), "strip": strip,
            "system_name": system_name, "win_w": win_w,
            "stamp": time.monotonic(),
        }
        _ui[0] = ui
        mouse = pygame.mouse.get_pos()

        def draw(draw_ctx):
            screen = draw_ctx.screen
            hover_pos = pygame.mouse.get_pos()
            for key in row_keys:
                _draw_button(
                    pygame, self, screen, key, rects[key], ui, hover_pos)

        # The row only: the dropup strip and tooltips are drawn by _on_draw,
        # above the game's windows. entries holds the open flags, attention
        # dots and subtitles the row's look depends on.
        state_key = (
            win_w, win_h, self._uk, system_name,
            tuple(sorted(entries.items())), mouse,
        )
        self._draw_retained_gpu_layer("hud:launchers", ctx, state_key, draw)

        # Drawn outside the retained/cached layer above: a toast animates
        # (fades) purely with the passage of time, which the cache's
        # state_key has no way to express, so it has to be blitted fresh
        # every frame while active instead of going through that cache.
        _draw_toast(pygame, self, ctx, rects.get("galaxy"))

    def patched(self, ctx, *args, **kwargs):
        # A game update that renames anything read above must not take the
        # whole HUD row down: log once and draw the game's own row instead.
        if not _icons_failed[0]:
            try:
                return _draw_hud_launchers_icons(self, ctx)
            except Exception:
                _icons_failed[0] = True
                _item_rects.clear()
                _dropup_rects.clear()
                _dropup_strip[0] = None
                _ui[0] = None
                _log.exception(
                    "menu-icons: icon row failed; using the game's own row")
        return _original_draw_hud_launchers(self, ctx, *args, **kwargs)

    return patched


_icons_failed = [False]


def _shop_icon():
    """The game's own premium-currency gem (premium_shop.gem_surface), the
    mark the native Menu's SHOP row carries."""
    module = _game_module("premium_shop")
    try:
        return module.gem_surface(22) if module is not None else None
    except Exception:
        _warn_once("gem", "premium_shop.gem_surface failed; Shop has no icon")
        return None


def _draw_toast(pygame, self, ctx, anchor_rect):
    text = _toast["text"]
    if not text or anchor_rect is None:
        return
    age = time.time() - _toast["shown_at"]
    if age >= _TOAST_LIFE:
        _toast["text"] = None
        return
    remaining = _TOAST_LIFE - age
    alpha = 255 if remaining > _TOAST_FADE else int(
        255 * (remaining / _TOAST_FADE))

    screen = ctx.screen
    font = self._instrument_font(11, bold=True)
    label = font.render(text, True, (228, 237, 250))

    pad_x, pad_y = self._s(10), self._s(6)
    box_w = label.get_width() + pad_x * 2
    box_h = label.get_height() + pad_y * 2
    box = pygame.Rect(0, 0, box_w, box_h)
    box.midbottom = (anchor_rect.centerx, anchor_rect.top - self._s(8))
    box.left = max(self._s(4), min(box.left, ctx.win_w - box.width - self._s(4)))

    surf = pygame.Surface((box.width, box.height), pygame.SRCALPHA)
    pygame.draw.rect(
        surf, (16, 34, 58, alpha), surf.get_rect(), border_radius=self._s(4))
    pygame.draw.rect(
        surf, (95, 170, 255, alpha), surf.get_rect(), 1,
        border_radius=self._s(4))
    label.set_alpha(alpha)
    surf.blit(label, (pad_x, pad_y))
    screen.blit(surf, box.topleft)


def _on_startup(host, pygame, screen):
    global _original_draw_hud_launchers, _patched_class

    _load_icons(pygame, _install_path[0])

    cls = type(host)
    _patched_class = cls
    _original_draw_hud_launchers = getattr(cls, "_draw_hud_launchers", None)
    if _original_draw_hud_launchers is None:
        _log.warning("menu-icons: _draw_hud_launchers missing; not patched")
        return
    cls._draw_hud_launchers = _make_draw_hud_launchers(pygame)


def _copy_system_name(host):
    name = _current_system_name[0] or "Unknown System"
    ok = host._set_clipboard_text(name)
    _toast["text"] = (
        f"Copied \"{name}\"" if ok else "Clipboard unavailable — copy failed")
    _toast["shown_at"] = time.time()


def _close_dropup():
    _open_category[0] = None


def _on_event(host, pygame, event, screen):
    if event.type == pygame.KEYDOWN:
        # Escape stays game-owned; just fold the dropup away alongside it.
        if event.key == pygame.K_ESCAPE:
            _close_dropup()
        return None
    if event.type != pygame.MOUSEBUTTONDOWN:
        return None
    # Bail out during full-screen modals the native row would also have
    # been blocked behind, so this doesn't click through them.
    if (getattr(host, "_disconnected", False)
            or getattr(host, "_esc_menu_open", False)
            or getattr(host, "_options_open", False)):
        _close_dropup()
        return None
    pos = event.pos
    if event.button == 3:
        rect = _item_rects.get("galaxy")
        if rect is not None and rect.collidepoint(pos):
            _copy_system_name(host)
        return None
    if event.button != 1:
        return None

    # Returning True consumes the click so the game's own handling never also
    # sees it (a dropup floats over whatever window happens to be below it).
    if _open_category[0] is not None:
        for key, rect in _dropup_rects.items():
            if rect.collidepoint(pos):
                _close_dropup()
                try:
                    _ACTIONS[key](host)
                except Exception:
                    _log.exception("menu-icons: %s action failed", key)
                return True
    for key, rect in _item_rects.items():
        if key.startswith("cat:") and rect.collidepoint(pos):
            cat_key = key[4:]
            _open_category[0] = (
                None if _open_category[0] == cat_key else cat_key)
            return True
    if _open_category[0] is not None:
        strip = _dropup_strip[0]
        if strip is not None and strip.collidepoint(pos):
            return True          # padding between icons: swallow, stay open
        _close_dropup()          # click elsewhere: fold away, let it through

    for key in _MOD_OWNED_KEYS:
        rect = _item_rects.get(key)
        if rect is not None and rect.collidepoint(pos):
            _ACTIONS[key](host)
            return None
    return None


def _on_shutdown(**_kwargs):
    if _patched_class is not None and _original_draw_hud_launchers is not None:
        _patched_class._draw_hud_launchers = _original_draw_hud_launchers


def apply(api):
    global _log
    _install_path[0] = api.install_path
    _log = api.logger
    api.on("client.startup", _on_startup)
    api.on("client.frame.begin", _on_frame_begin)
    api.on("client.event", _on_event)
    api.on("client.draw", _on_draw)
    api.on("loader.shutdown", _on_shutdown)
    api.logger.info("menu-icons ready, waiting for client.startup")
