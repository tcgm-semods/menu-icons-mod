"""Menu Icons: replaces the entire bottom-right HUD row (Menu/Minimap/Squad/
Galaxy + the Menu dropup's items) with one row of uniform icon buttons.
No dropup is drawn at all -- everything lives directly in the row.

Approach:
  * Patches SolarSystemWindow._draw_hud_launchers (render_mixin.py) to draw
    nine icon buttons instead of the original four text buttons.
  * Still publishes self._galaxy_map_btn_rect / _squad_btn_rect /
    _minimap_btn_rect exactly as the original did, so the game's own,
    untouched click-handling code for those three keeps working unchanged.
  * self._menu_btn_rect is set to None every frame, so the original
    "toggle _menu_open" click handler never fires and the now-orphaned
    dropup (_draw_menu_popup) never draws -- no need to patch it at all.
  * The former dropup items (stats/friends/missions/run summary/coalition/
    attendance/market/shop) have no ungated native click path once _menu_open
    can never become true, so this mod owns their clicks itself via the
    client.event hook.
  * Right-clicking the Universe Map (galaxy) icon copies the current system
    name to the clipboard and pops a small self-drawn toast confirming it.
    Native code only ever looks at button 1 (left-click) for this whole
    launcher row, so button 3 is free for this mod to use outright -- no
    race with native handling to work around here.

Game 0.5.82 added two Menu entries (Client.py _draw_menu_popup): RUN SUMMARY
(run_summary_gui.toggle) and SHOP (premium_shop.open_shop, only listed while
the server enables the shop). Both now get a button here too; the Shop one
only appears while host._premium_shop.enabled, like the native row, and uses
the game's own gold gem as its icon.
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
    "friends": "users.png",
    "missions": "flag.png",
    "runs": "clipboard.png",
    "coalition": "shield.png",
    "attendance": "calendar.png",
    "market": "shopping_cart.png",
    "options": "settings.png",
}

# Items this mod owns clicks for (galaxy/squad/minimap keep native handling).
_MOD_OWNED_KEYS = (
    "stats", "friends", "missions", "runs", "coalition", "attendance",
    "market", "shop", "options")

_icons = {}
_item_rects = {}
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
    """A loose game module (run_summary_gui, premium_shop), or None. Client.py
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
    "attendance": _open_attendance,
    "market": _open_market,
    "shop": _open_shop,
    "options": _open_options,
}


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
        shop_enabled, shop_open = _shop_state(self)

        # (key, active, attention, tooltip title, tooltip subtitle or None)
        items = [
            ("galaxy", self._galaxy_map_open, False, "Universe Map", None),
            ("squad", self._squad_open, squad_has_members, "Squad", None),
            ("minimap", self._mm_open, False, "Minimap", None),
            ("stats", self._stats_open, False, "Stats", None),
            ("friends", self._friends_open, bool(friends_pending),
             "Friends", None),
            ("missions", self._mission_log_open, bool(mission_ready),
             "Missions", None),
        ]
        if runs is not None:
            items.append(("runs", runs[0], runs[1], "Run Summary",
                          runs[2] or None))
        items += [
            ("coalition", self._coalition_open, False, "Coalition", None),
            ("attendance", self._attendance_gui.open, bool(attendance_ready),
             "Attendance Rewards", None),
            ("market", self._market_open, False, "Global Market", None),
        ]
        if shop_enabled:
            items.append(("shop", shop_open, False, "Shop", None))
        items.append(
            ("options", self._esc_menu_open, False, "Options", None))

        total_w = (
            sum(widths.get(key, btn) for key, *_r in items)
            + (len(items) - 1) * gap)
        start_x = win_w - pad - total_w

        rects = {}
        cursor_x = start_x
        for key, _active, _attention, _title, _sub in items:
            w = widths.get(key, btn)
            rects[key] = pygame.Rect(cursor_x, button_y, w, btn)
            cursor_x += w + gap

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

        mouse = pygame.mouse.get_pos()

        def draw(draw_ctx):
            screen = draw_ctx.screen
            hover_pos = pygame.mouse.get_pos()
            hovered = None
            for key, active, attention, title, subtitle in items:
                rect = rects[key]
                hover = rect.collidepoint(hover_pos)
                fill = ((18, 58, 94) if active else
                        (18, 42, 72) if hover else (11, 24, 43))
                border = ((95, 170, 255) if (active or hover)
                          else (56, 88, 138))
                pygame.draw.rect(screen, fill, rect, border_radius=self._s(4))
                pygame.draw.rect(
                    screen, border, rect, 1, border_radius=self._s(4))

                icon = _icons.get(key)
                if key == "galaxy":
                    icon_pad = self._s(10)
                    if icon is not None:
                        icon_rect = icon.get_rect(
                            midleft=(rect.left + icon_pad, rect.centery))
                        screen.blit(icon, icon_rect)
                        text_left = icon_rect.right + self._s(6)
                    else:
                        text_left = rect.left + icon_pad
                    name_font = self._instrument_font(11, bold=True)
                    name_color = (228, 237, 250) if (active or hover) \
                        else (185, 215, 250)
                    name_label = self._cached_fitted_label(
                        str(system_name), name_color, name_font,
                        rect.right - self._s(8) - text_left)
                    screen.blit(name_label, (
                        text_left,
                        rect.centery - name_label.get_height() // 2))
                elif key == "shop":
                    gem = _shop_icon()
                    if gem is not None:
                        screen.blit(gem, gem.get_rect(center=rect.center))
                elif icon is not None:
                    screen.blit(icon, icon.get_rect(center=rect.center))

                if attention:
                    dot = self._s(8)
                    dot_rect = pygame.Rect(
                        rect.right - dot - self._s(2), rect.top + self._s(2),
                        dot, dot)
                    pygame.draw.ellipse(screen, (255, 92, 72), dot_rect)
                    pygame.draw.ellipse(screen, (8, 18, 33), dot_rect, 1)

                if hover:
                    hovered = (title, subtitle, rect)

            if hovered is not None:
                title, subtitle, rect = hovered
                title_font = self._instrument_font(10, bold=True)
                title_label = title_font.render(
                    title.upper(), True, (228, 237, 250))
                sub_label = None
                if subtitle:
                    sub_font = self._instrument_font(9, bold=False)
                    sub_label = sub_font.render(
                        subtitle.upper(), True, (149, 188, 235))

                pad_t = self._s(6)
                line_gap = self._s(2)
                content_w = title_label.get_width()
                content_h = title_label.get_height()
                if sub_label is not None:
                    content_w = max(content_w, sub_label.get_width())
                    content_h += line_gap + sub_label.get_height()

                box = pygame.Rect(
                    0, 0, content_w + pad_t * 2, content_h + pad_t * 2)
                box.midbottom = (rect.centerx, rect.top - self._s(4))
                box.left = max(
                    self._s(4), min(box.left, win_w - box.width - self._s(4)))
                pygame.draw.rect(
                    screen, (8, 18, 33), box, border_radius=self._s(3))
                pygame.draw.rect(
                    screen, (56, 88, 138), box, 1, border_radius=self._s(3))
                screen.blit(title_label, (box.x + pad_t, box.y + pad_t))
                if sub_label is not None:
                    screen.blit(sub_label, (
                        box.x + pad_t,
                        box.y + pad_t + title_label.get_height() + line_gap))

        state_key = (
            win_w, win_h, self._uk, system_name, int(attendance_ready),
            int(mission_ready), int(friends_pending),
            bool(self._galaxy_map_open), bool(self._squad_open),
            bool(self._mm_open), bool(self._stats_open),
            bool(self._friends_open), bool(self._mission_log_open),
            bool(self._coalition_open), bool(self._attendance_gui.open),
            bool(self._market_open), bool(self._esc_menu_open),
            squad_has_members, runs, shop_enabled, shop_open, mouse,
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


def _on_event(host, pygame, event, screen):
    if event.type != pygame.MOUSEBUTTONDOWN:
        return
    # Bail out during full-screen modals the native row would also have
    # been blocked behind, so this doesn't click through them.
    if (getattr(host, "_disconnected", False)
            or getattr(host, "_esc_menu_open", False)
            or getattr(host, "_options_open", False)):
        return
    pos = event.pos
    if event.button == 3:
        rect = _item_rects.get("galaxy")
        if rect is not None and rect.collidepoint(pos):
            _copy_system_name(host)
        return
    if event.button != 1:
        return
    for key in _MOD_OWNED_KEYS:
        rect = _item_rects.get(key)
        if rect is not None and rect.collidepoint(pos):
            _ACTIONS[key](host)
            return


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
    api.on("loader.shutdown", _on_shutdown)
    api.logger.info("menu-icons ready, waiting for client.startup")
