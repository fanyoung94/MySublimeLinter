from collections import defaultdict, ChainMap
import sublime

from .lint import persist, events
from .lint import style as style_stores
from .lint.const import PROTECTED_REGIONS_KEY, INBUILT_ICONS


UNDERLINE_FLAGS = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE | sublime.DRAW_EMPTY_AS_OVERWRITE

MARK_STYLES = {
    'outline': sublime.DRAW_NO_FILL,
    'fill': sublime.DRAW_NO_OUTLINE,
    'solid_underline': sublime.DRAW_SOLID_UNDERLINE | UNDERLINE_FLAGS,
    'squiggly_underline': sublime.DRAW_SQUIGGLY_UNDERLINE | UNDERLINE_FLAGS,
    'stippled_underline': sublime.DRAW_STIPPLED_UNDERLINE | UNDERLINE_FLAGS,
    'none': sublime.HIDDEN
}

highlight_store = style_stores.HighlightStyleStore()
REGION_KEYS = 'SL.{}.region_keys'


def remember_region_keys(view, keys):
    view.settings().set(REGION_KEYS.format(view.id()), keys)


def get_regions_keys(view):
    return set(view.settings().get(REGION_KEYS.format(view.id()), []))


def clear_view(view):
    for key in get_regions_keys(view):
        view.erase_regions(key)

    remember_region_keys(view, [])


def plugin_unloaded():
    events.off(on_finished_linting)


@events.on(events.FINISHED_LINTING)
def on_finished_linting(buffer_id):
    views = list(all_views_into_buffer(buffer_id))
    if not views:
        return

    errors = persist.errors[buffer_id]
    marks, lines = prepare_data(views[0], errors)

    for view in views:
        draw(view, marks, lines)


def all_views_into_buffer(buffer_id):
    for window in sublime.windows():
        for view in window.views():
            if view.buffer_id() == buffer_id:
                yield view


def prepare_data(view, errors):
    errors_augmented = []
    for error in errors:
        style = get_base_error_style(**error)
        if not highlight_store.has_style(style):  # really?
            continue

        priority = int(highlight_store.get(style).get('priority', 0))
        errors_augmented.append(
            ChainMap({'style': style, 'priority': priority}, error))

    highlight_regions = prepare_highlights_data(view, errors_augmented)
    gutter_regions = prepare_gutter_data(view, errors_augmented)
    return highlight_regions, gutter_regions


def get_base_error_style(linter, code, error_type, **kwargs):
    store = style_stores.get_linter_style_store(linter)
    return store.get_style(code, error_type)


def prepare_gutter_data(view, errors):
    errors_by_line = defaultdict(list)
    for error in errors:
        errors_by_line[error['line']].append(error)

    # Since, there can only be one gutter mark per line, we have to select
    # one 'winning' error from all the errors on that line
    filtered_errors = []
    for line, errors in errors_by_line.items():
        # We're lucky here that 'error' comes before 'warning'
        head = sorted(errors, key=lambda e: (e['error_type'], -e['priority']))[0]
        filtered_errors.append(head)

    # Compute the icon and scope for the gutter mark from the error.
    # Drop lines for which we don't get a value or for which the user
    # specified 'none'
    by_id = defaultdict(list)
    for error in filtered_errors:
        icon = get_icon(**error)
        if not icon or icon == 'none':
            continue

        scope = get_icon_scope(icon, error)

        pos = get_line_start(view, error['line'])
        region = sublime.Region(pos, pos)

        # We group towards the optimal sublime API usage:
        #   view.add_regions(uuid(), [region], scope, icon)
        id = (scope, icon)
        by_id[id].append(region)

    # Exchange the `id` with a regular region_id which is a unique string, so
    # uuid() would be candidate here, that can be reused for efficient updates.
    by_region_id = {}
    for (scope, icon), regions in by_id.items():
        region_id = 'SL.Gutter.{}.{}'.format(scope, icon)
        by_region_id[region_id] = (scope, icon, regions)

    return by_region_id


def prepare_highlights_data(view, errors):
    # We can only one highlight per exact position, so we first group per
    # position.
    by_position = defaultdict(list)
    for error in errors:
        by_position[(error['line'], error['start'], error['end'])].append(error)

    filtered_errors = []
    for pos, errors in by_position.items():
        # If we have multiple 'problems' here, 'error' takes precedence over
        # 'warning'. We're lucky again that 'error' comes before 'warning'.
        head = sorted(errors, key=lambda e: e['error_type'])[0]
        filtered_errors.append(head)

    by_id = defaultdict(list)
    for error in filtered_errors:
        scope = get_scope(**error)
        mark_style = get_mark_style(**error)
        flags = MARK_STYLES[mark_style]
        if not persist.settings.get('show_marks_in_minimap'):
                flags |= sublime.HIDE_ON_MINIMAP

        line_start = get_line_start(view, error['line'])
        region = sublime.Region(line_start + error['start'], line_start + error['end'])

        # We group towards the sublime API usage
        #   view.add_regions(uuid(), regions, scope, flags)
        id = (scope, flags)
        by_id[id].append(region)

    # Exchange the `id` with a regular sublime region_id which is a unique
    # string. Generally, uuid() would suffice, but generate an id here for
    # efficient updates.
    by_region_id = {}
    for (scope, flags), regions in by_id.items():
        region_id = 'SL.Highlights.{}.{}'.format(scope, flags)
        by_region_id[region_id] = (scope, flags, regions)

    return by_region_id


def get_line_start(view, line):
    return view.text_point(line, 0)


def get_icon(style, error_type, **kwargs):
    return highlight_store.get_val('icon', style, error_type)


def get_scope(style, error_type, **kwargs):
    return highlight_store.get_val('scope', style, error_type)


def get_mark_style(style, error_type, **kwargs):
    return highlight_store.get_val('mark_style', style, error_type)


def get_icon_scope(icon, error):
    if style_stores.GUTTER_ICONS.get('colorize', True) or icon in INBUILT_ICONS:
        return get_scope(**error)
    else:
        return " "  # set scope to non-existent one


def draw(view, highlight_regions, gutter_regions):
    """
    Draw code and gutter marks in the given view.

    Error, warning and gutter marks are drawn with separate regions,
    since each one potentially needs a different color.

    """
    current_region_keys = get_regions_keys(view)
    new_regions_keys = list(highlight_regions.keys()) + list(gutter_regions.keys())
    if len(gutter_regions):
        new_regions_keys.append(PROTECTED_REGIONS_KEY)

    # remove unused regions
    for key in current_region_keys - set(new_regions_keys):
        view.erase_regions(key)

    # otherwise update (or create) regions

    for region_id, (scope, flags, regions) in highlight_regions.items():
        view.add_regions(region_id, regions, scope=scope, flags=flags)

    if persist.settings.has('gutter_theme'):
        protected_regions = []

        for region_id, (scope, icon, regions) in gutter_regions.items():
            view.add_regions(region_id, regions, scope=scope, icon=icon)
            protected_regions.extend(regions)

        # overlaying all gutter regions with common invisible one,
        # to create unified handle for GitGutter and other plugins
        # flag might not be neccessary
        if protected_regions:
            view.add_regions(
                PROTECTED_REGIONS_KEY,
                protected_regions,
                flags=sublime.HIDDEN
            )

    # persisting region keys for later clearance
    remember_region_keys(view, new_regions_keys)
