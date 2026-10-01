"""
Ideascape Stock Reports - InvenTree plugin
==========================================

One plugin that powers two Stock Location reports:

1. Project Stock Cost Report  (project_stock_cost_report.html)
   Adds the totals at the bottom: total_value, total_value_currency,
   total_value_mixed, total_value_by_currency, missing_price_count,
   total_value_available.

2. Stock Movement Summary     (stock_movement_report.html)
   Adds ``stock_movement``: per store (stock location), at every level,
   per month - opening, stock in, stock out and closing quantity and value.
   It is only calculated when a template uses it.

How the stock movement history is rebuilt
-----------------------------------------
InvenTree does not store month-end balances, so the plugin replays every
stock item's tracking history (receipts, transfers, splits, stock counts,
adds / removes, shipments, returns) to work out where each item was, and
how much of it there was, at any point in time. Values use each stock
item's purchase price (converted to the default currency if needed).
The history is anchored to each item's actual current state: anything the
history doesn't explain is shown in the current month as an "Adjustment",
so closing figures always match what is in InvenTree today.

Install: see INSTALL.md (copy this file into the InvenTree plugins folder,
or install the package from Git via Settings -> Plugins -> Install Plugin),
then enable "Ideascape Stock Reports" under Settings -> Plugins.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import cached_property

from plugin import InvenTreePlugin
from plugin.mixins import ReportMixin, SettingsMixin

# ---------------------------------------------------------------------------
# InvenTree stock history codes (stock.status_codes.StockHistoryCode).
# Hard-coded as integers so the plugin keeps working across InvenTree versions.
# ---------------------------------------------------------------------------
CREATED = 1
STOCK_SERIALIZED = 13
STOCK_MOVE = 20
INSTALLED_INTO_ASSEMBLY = 30
INSTALLED_CHILD_ITEM = 35
REMOVED_CHILD_ITEM = 36
SPLIT_FROM_PARENT = 40
SPLIT_CHILD_ITEM = 42
MERGED_STOCK_ITEMS = 45
CREATED_FROM_DISASSEMBLY = 47
BUILD_OUTPUT_CREATED = 50
BUILD_CONSUMED = 57
SHIPPED_AGAINST_SALES_ORDER = 60
RECEIVED_AGAINST_PURCHASE_ORDER = 70
SENT_TO_CUSTOMER = 100

# Internal pseudo-code: quantity taken from an item by a "merge on transfer"
# (InvenTree records that on the receiving item only).
_MERGED_AWAY = -100

# Entries which mark the start of a stock item's life
BIRTH_CODES = {
    CREATED,
    SPLIT_FROM_PARENT,
    CREATED_FROM_DISASSEMBLY,
    BUILD_OUTPUT_CREATED,
    RECEIVED_AGAINST_PURCHASE_ORDER,
}

# Entries after which the item is no longer in any store
OUT_CODES = {
    INSTALLED_INTO_ASSEMBLY,
    BUILD_CONSUMED,
    SHIPPED_AGAINST_SALES_ORDER,
    SENT_TO_CUSTOMER,
}

# Entries whose 'quantity' / 'location' describe *another* item
FOREIGN_QUANTITY_CODES = {INSTALLED_CHILD_ITEM, REMOVED_CHILD_ITEM}
FOREIGN_LOCATION_CODES = {INSTALLED_CHILD_ITEM, REMOVED_CHILD_ITEM, SPLIT_CHILD_ITEM}

UNKNOWN = -1  # location not recorded in history
ALL_STORES = 0  # key of the synthetic "All stores" row
ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Pure-Python history replay (no Django imports - unit testable)
# ---------------------------------------------------------------------------
def to_decimal(value):
    """Convert a tracking delta value to Decimal (None if missing / invalid)."""
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def to_location(value):
    """Convert a tracking delta location value to a location pk (or None)."""
    if value in (None, ''):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def apply_entry(code, deltas, loc, qty):
    """Return the (location, quantity) of an item after a tracking entry."""
    if code in FOREIGN_QUANTITY_CODES:
        return loc, qty

    if code in OUT_CODES:
        return None, qty

    if 'location' in deltas and code not in FOREIGN_LOCATION_CODES:
        loc = to_location(deltas.get('location'))

    quantity = to_decimal(deltas.get('quantity'))
    removed = to_decimal(deltas.get('removed'))
    added = to_decimal(deltas.get('added'))

    if code == _MERGED_AWAY:
        qty -= removed or ZERO
    elif quantity is not None:
        qty = quantity  # InvenTree records the resulting quantity
    elif removed is not None:
        qty -= removed
    elif added is not None:
        qty += added

    return loc, qty


NORMAL, SILENT, ADJUST = 'normal', 'silent', 'adjust'


def location_at(changes, when):
    """Location of an item at a point in time, from its change list."""
    result = None
    for change in changes or ():
        if change[0] is None or change[0] <= when:
            result = change[1]
        else:
            break
    return result


def build_timeline(entries, current_loc, current_qty, now, parent_changes=None):
    """Rebuild one stock item's (location, quantity) over time.

    Arguments:
        entries: list of (date, code, deltas[, meta]) sorted by date, where the
            optional meta dict may contain:
              'silent': True  - quantity passed to another item (split / merge);
                                the other item records it as a transfer
              'origin': (item_pk, qty) - quantity came from another item
        current_loc / current_qty: the item's actual state today
        now: report generation time
        parent_changes: change list of the parent item (for split items)

    Returns:
        (changes, flags) where changes is a list of
        (date, location, quantity, kind, origin_item, origin_qty) - the state
        from that date onwards (date None = since before any recorded
        history); kind is NORMAL, SILENT or ADJUST - and flags is a set of
        data-quality markers.
    """
    flags = set()
    current_qty = to_decimal(current_qty) or ZERO

    if not entries:
        return [(None, current_loc, current_qty, NORMAL, None, None)], flags

    # Start at the last "birth" entry - anything earlier was copied from
    # another item (InvenTree's "duplicate with history" option)
    start = 0
    for idx, entry in enumerate(entries):
        if entry[1] in BIRTH_CODES:
            start = idx

    has_birth = entries[start][1] in BIRTH_CODES
    birth_date = entries[start][0]

    changes = []
    loc, qty = UNKNOWN, current_qty

    for idx in range(start, len(entries)):
        date, code, deltas = entries[idx][:3]
        meta = entries[idx][3] if len(entries[idx]) > 3 else {}
        deltas = deltas if isinstance(deltas, dict) else {}

        if idx == start:
            loc = UNKNOWN
            quantity = to_decimal(deltas.get('quantity'))
            qty = quantity if quantity is not None else current_qty

        loc, qty = apply_entry(code, deltas, loc, qty)

        # No birth entry: the first known state applies from the beginning
        change_date = None if (idx == start and not has_birth) else date
        origin_item, origin_qty = meta.get('origin') or (None, None)
        kind = SILENT if meta.get('silent') else NORMAL
        changes.append([change_date, loc, qty, kind, origin_item, origin_qty])

    # Resolve an unrecorded starting location
    if any(change[1] == UNKNOWN for change in changes):
        if changes[-1][1] == UNKNOWN:
            # Never moved since - it has been where it is now all along
            resolved = current_loc
        else:
            # Moved later: best estimate is the parent's location at the split
            resolved = (
                location_at(parent_changes, birth_date) if parent_changes else None
            )
            if resolved in (None, UNKNOWN):
                resolved = None
                flags.add('unknown_location')
            else:
                flags.add('estimated_location')

        for change in changes:
            if change[1] == UNKNOWN:
                change[1] = resolved

    # Anchor to the item's actual current state
    if _contribution(changes[-1][1], changes[-1][2]) != _contribution(
        current_loc, current_qty
    ):
        changes.append([now, current_loc, current_qty, ADJUST, None, None])
        flags.add('adjusted')

    return [tuple(change) for change in changes], flags


def _contribution(loc, qty):
    """The (location, quantity) an item contributes to store totals."""
    if loc in (None, UNKNOWN) or qty is None or qty <= 0:
        return (None, ZERO)
    return (loc, qty)


def compute_movements(
    locations,
    roots,
    include_all_row,
    items,
    tracking,
    months,
    month_key,
    now,
    max_depth=0,
    hide_empty=True,
):
    """Compute rolled-up monthly movements for a set of stores.

    Arguments:
        locations: {pk: {'parent': pk|None, 'name': str, 'path': str}} (all locations)
        roots: location pks the report covers (each with all descendants)
        include_all_row: add a synthetic "All stores" row covering everything
        items: list of {'id', 'parent', 'location', 'quantity', 'price'}
        tracking: {item_id: [(date, code, deltas), ...]} sorted by date
        months: list of (key, label, start_datetime), oldest first
        month_key: function datetime -> key
        now: report generation time
        max_depth: only list stores down to this level (0 = all)
        hide_empty: hide stores with no stock and no movement in the period
    """
    period_start = months[0][2]
    month_index = {key: idx for idx, (key, _label, _start) in enumerate(months)}
    n_months = len(months)

    # --- Which stores are in the report --------------------------------------
    children = {}
    for pk, info in locations.items():
        children.setdefault(info['parent'], []).append(pk)
    for kids in children.values():
        kids.sort(key=lambda pk: (locations[pk]['name'] or '').lower())

    nodes = set()
    stack = list(roots)
    while stack:
        pk = stack.pop()
        if pk in locations and pk not in nodes:
            nodes.add(pk)
            stack.extend(children.get(pk, []))
    if include_all_row:
        nodes.add(ALL_STORES)

    ancestor_cache = {}

    def ancestors(loc):
        """Report stores which contain a location (itself included)."""
        if loc in (None, UNKNOWN):
            return ()
        if loc not in ancestor_cache:
            found = []
            seen = set()
            current = loc
            while current is not None and current in locations and current not in seen:
                seen.add(current)
                if current in nodes:
                    found.append(current)
                current = locations[current]['parent']
            if include_all_row and loc in locations:
                found.append(ALL_STORES)
            ancestor_cache[loc] = tuple(found)
        return ancestor_cache[loc]

    # --- Pair up splits and merges so they count as transfers ---------------
    # A partial transfer splits an item: the parent loses quantity and a new
    # child item appears at the destination. Recording those separately would
    # show an internal transfer as both "in" and "out" at parent-store level,
    # so the parent's reduction is made silent and the child's arrival is
    # treated as a transfer from the parent's location. Merges likewise.
    item_ids = {item['id'] for item in items}
    parent_of = {item['id']: item['parent'] for item in items}
    tracking = {
        pk: [(e[0], e[1], e[2] if isinstance(e[2], dict) else {}, {}) for e in entries]
        for pk, entries in tracking.items()
    }

    serialized_parents = set()
    for pk, entries in tracking.items():
        for date, code, deltas, meta in entries:
            if code == SPLIT_FROM_PARENT:
                parent = to_location(deltas.get('stockitem')) or parent_of.get(pk)
                if parent in item_ids and parent != pk:
                    qty = to_decimal(deltas.get('quantity'))
                    if qty:
                        meta['origin'] = (parent, qty)
                        serialized_parents.add(parent)

    for pk, entries in list(tracking.items()):
        for date, code, deltas, meta in entries:
            if code == SPLIT_CHILD_ITEM:
                child = to_location(deltas.get('stockitem'))
                if child in item_ids and child != pk:
                    meta['silent'] = True
            elif code == STOCK_SERIALIZED and pk in serialized_parents:
                meta['silent'] = True
            elif code == MERGED_STOCK_ITEMS:
                source = to_location(deltas.get('stockitem'))
                added = to_decimal(deltas.get('added'))
                if source and source != pk and source in item_ids and added:
                    # InvenTree only logs the merge on the receiving item
                    meta['origin'] = (source, added)
                    tracking.setdefault(source, []).append(
                        (date, _MERGED_AWAY, {'removed': added}, {'silent': True})
                    )

    for entries in tracking.values():
        entries.sort(key=lambda entry: entry[0])

    # --- Accumulate --------------------------------------------------------------
    def blank():
        return {
            'opening': [ZERO, ZERO],
            'in': [[ZERO, ZERO] for _ in range(n_months)],
            'out': [[ZERO, ZERO] for _ in range(n_months)],
            'adj': [[ZERO, ZERO] for _ in range(n_months)],
        }

    stats = {node: blank() for node in nodes}
    actual_close = {node: [ZERO, ZERO] for node in nodes}
    counts = {
        'items': 0,
        'missing_price': 0,
        'adjusted': 0,
        'estimated_location': 0,
        'unknown_location': 0,
    }

    unit_price = {
        item['id']: item['price'] if item['price'] is not None else ZERO
        for item in items
    }

    # Pass 1: rebuild every item's timeline (parents before children)
    timelines, item_flags = {}, {}
    for item in sorted(items, key=lambda it: it['id']):
        changes, flags = build_timeline(
            tracking.get(item['id'], []),
            item['location'],
            item['quantity'],
            now,
            parent_changes=timelines.get(item['parent']),
        )
        timelines[item['id']] = changes
        item_flags[item['id']] = flags

    def add(bucket, qty, value):
        bucket[0] += qty
        bucket[1] += value

    # Pass 2: turn state changes into store movements
    for item in items:
        pk = item['id']
        unit = unit_price[pk]
        touched = False

        prev_loc, prev_qty = None, ZERO
        opening_done = False

        for date, loc, qty, kind, origin_item, origin_qty in timelines[pk]:
            new_loc, new_qty = _contribution(loc, qty)

            if date is None or date < period_start:
                prev_loc, prev_qty = new_loc, new_qty
                continue

            if not opening_done:
                opening_done = True
                for node in ancestors(prev_loc):
                    add(stats[node]['opening'], prev_qty, prev_qty * unit)
                    touched = True

            if kind == SILENT:
                # Recorded as a transfer by the item that received the quantity
                prev_loc, prev_qty = new_loc, new_qty
                continue

            key = month_key(date)
            m = month_index.get(key, n_months - 1 if key > months[-1][0] else None)

            if m is not None:
                origin_loc = None
                origin_unit = ZERO
                if origin_item is not None and origin_qty:
                    origin_loc = location_at(timelines.get(origin_item), date)
                    origin_loc, origin_qty = _contribution(origin_loc, origin_qty)
                    origin_unit = unit_price.get(origin_item, ZERO)

                before_nodes = ancestors(prev_loc)
                after_nodes = ancestors(new_loc)
                origin_nodes = ancestors(origin_loc)

                for node in set(before_nodes) | set(after_nodes) | set(origin_nodes):
                    before_q = before_v = after_q = after_v = ZERO
                    if node in before_nodes:
                        before_q += prev_qty
                        before_v += prev_qty * unit
                    if node in origin_nodes:
                        before_q += origin_qty
                        before_v += origin_qty * origin_unit
                    if node in after_nodes:
                        after_q, after_v = new_qty, new_qty * unit

                    dq, dv = after_q - before_q, after_v - before_v
                    if dq == 0 and dv == 0:
                        continue
                    touched = True
                    if kind == ADJUST or dq == 0:
                        add(stats[node]['adj'][m], dq, dv)
                    elif dq > 0:
                        add(stats[node]['in'][m], dq, dv)
                    else:
                        add(stats[node]['out'][m], -dq, -dv)

            prev_loc, prev_qty = new_loc, new_qty

        if not opening_done:
            for node in ancestors(prev_loc):
                add(stats[node]['opening'], prev_qty, prev_qty * unit)
                touched = True

        # What this item actually contributes today
        for node in ancestors(prev_loc):
            add(actual_close[node], prev_qty, prev_qty * unit)

        if touched or ancestors(item['location']):
            counts['items'] += 1
            if item['price'] is None and touched:
                counts['missing_price'] += 1
            for flag in item_flags[pk]:
                counts[flag] += 1

    # Closing figures must equal what is in InvenTree today - any remaining
    # difference (e.g. price differences on merged stock) goes to adjustments
    for node, data in stats.items():
        close_q, close_v = data['opening']
        for m in range(n_months):
            close_q += data['in'][m][0] - data['out'][m][0] + data['adj'][m][0]
            close_v += data['in'][m][1] - data['out'][m][1] + data['adj'][m][1]
        add(
            data['adj'][n_months - 1],
            actual_close[node][0] - close_q,
            actual_close[node][1] - close_v,
        )

    # --- Build output rows (tree order) --------------------------------------
    ordered = []
    if include_all_row:
        ordered.append((ALL_STORES, 0))
    base_level = 1 if include_all_row else 0

    def walk(pk, level):
        ordered.append((pk, level))
        for child in children.get(pk, []):
            walk(child, level + 1)

    for root in sorted(roots, key=lambda pk: (locations.get(pk, {}).get('name') or '').lower()):
        if root in locations:
            walk(root, base_level)

    top_level = {ALL_STORES} if include_all_row else set(roots)
    rows = []
    has_adjustments = False

    for pk, level in ordered:
        if max_depth and level > max_depth:
            continue

        data = stats[pk]
        opening_qty, opening_value = data['opening']
        closing_qty, closing_value = opening_qty, opening_value
        month_rows = []
        totals = {k: [ZERO, ZERO] for k in ('in', 'out', 'adj')}
        active = opening_qty != 0 or opening_value != 0

        for m, (_key, label, _start) in enumerate(months):
            start_qty, start_value = closing_qty, closing_value
            in_q, in_v = data['in'][m]
            out_q, out_v = data['out'][m]
            adj_q, adj_v = data['adj'][m]
            closing_qty = start_qty + in_q - out_q + adj_q
            closing_value = start_value + in_v - out_v + adj_v
            for k, (q, v) in (('in', (in_q, in_v)), ('out', (out_q, out_v)), ('adj', (adj_q, adj_v))):
                totals[k][0] += q
                totals[k][1] += v
            if in_q or out_q or adj_q or in_v or out_v or adj_v:
                active = True
            if adj_q or adj_v:
                has_adjustments = True
            month_rows.append({
                'label': label,
                'opening_qty': start_qty,
                'opening_value': start_value,
                'in_qty': in_q,
                'in_value': in_v,
                'out_qty': out_q,
                'out_value': out_v,
                'adj_qty': adj_q,
                'adj_value': adj_v,
                'closing_qty': closing_qty,
                'closing_value': closing_value,
            })

        if hide_empty and not active and pk not in top_level:
            continue

        if pk == ALL_STORES:
            name, path = 'All stores', 'All stores'
        else:
            name, path = locations[pk]['name'], locations[pk]['path'] or locations[pk]['name']

        rows.append({
            'id': pk,
            'name': name,
            'path': path,
            'level': level,
            'indent': f'{1.5 + level * 4:.1f}',  # left padding in mm
            'months': month_rows,
            'period': {
                'opening_qty': opening_qty,
                'opening_value': opening_value,
                'in_qty': totals['in'][0],
                'in_value': totals['in'][1],
                'out_qty': totals['out'][0],
                'out_value': totals['out'][1],
                'adj_qty': totals['adj'][0],
                'adj_value': totals['adj'][1],
                'closing_qty': closing_qty,
                'closing_value': closing_value,
            },
        })

    return {
        'rows': rows,
        'months': [label for _key, label, _start in months],
        'has_adjustments': has_adjustments,
        'counts': counts,
    }


# ---------------------------------------------------------------------------
# InvenTree plugin
# ---------------------------------------------------------------------------
class StockMovementReport:
    """Lazily computed report data, exposed to templates as ``stock_movement``."""

    def __init__(self, plugin, location):
        """Store references - nothing is calculated yet."""
        self._plugin = plugin
        self._location = location

    @cached_property
    def data(self):
        """Calculate the report (once, on first use)."""
        try:
            return self._plugin.build_report(self._location)
        except Exception as exc:  # surface errors instead of silently blank output
            raise RuntimeError(f'Stock movement report failed: {exc!r}') from exc

    @property
    def rows(self):
        """One row per store, in tree order."""
        return self.data['rows']

    @property
    def months(self):
        """Month labels covered by the report."""
        return self.data['months']

    @property
    def has_adjustments(self):
        """True if any store has unexplained adjustments."""
        return self.data['has_adjustments']

    @property
    def counts(self):
        """Data quality counters."""
        return self.data['counts']

    @property
    def period_label(self):
        """E.g. 'Oct 2025 - Sep 2026'."""
        months = self.data['months']
        return f'{months[0]} - {months[-1]}' if months else ''

    @property
    def scope_label(self):
        """Which stores the report covers."""
        return self.data['scope']

    @property
    def currency(self):
        """Currency the values are expressed in."""
        return self.data['currency']


def location_cost_totals(location):
    """Totals for the Project Stock Cost Report (stock at a location and its sub-locations)."""
    totals_by_currency = {}
    missing_price_count = 0

    for item in location.get_stock_items():
        if not item.purchase_price:
            missing_price_count += 1
            continue
        currency = str(item.purchase_price.currency)
        line_value = Decimal(item.purchase_price.amount) * Decimal(item.quantity)
        totals_by_currency[currency] = totals_by_currency.get(currency, ZERO) + line_value

    result = {
        'total_value_available': True,
        'missing_price_count': missing_price_count,
        'total_value_mixed': len(totals_by_currency) > 1,
    }

    if len(totals_by_currency) > 1:
        # Never silently add different currencies together
        result['total_value_by_currency'] = totals_by_currency
    elif totals_by_currency:
        currency, total = next(iter(totals_by_currency.items()))
        result['total_value'] = total
        result['total_value_currency'] = currency
    else:
        result['total_value'] = ZERO
        result['total_value_currency'] = None

    return result


class IdeascapeStockReportsPlugin(SettingsMixin, ReportMixin, InvenTreePlugin):
    """Project stock cost totals and monthly stock movements per store."""

    NAME = 'IdeascapeStockReports'
    SLUG = 'ideascape-stock-reports'
    TITLE = 'Ideascape Stock Reports'
    DESCRIPTION = 'Project stock cost totals and monthly stock movement summary per store'
    VERSION = '2.0.0'
    AUTHOR = 'Ideascape'

    SETTINGS = {
        'REPORT_MONTHS': {
            'name': 'Months to report',
            'description': 'Number of months (including the current month) shown on the report',
            'default': 12,
            'validator': int,
        },
        'MAX_DEPTH': {
            'name': 'Store levels to list',
            'description': 'How many levels of sub-stores to list below the printed location (0 = all levels). Totals always include all levels.',
            'default': 0,
            'validator': int,
        },
        'HIDE_EMPTY': {
            'name': 'Hide inactive stores',
            'description': 'Leave out stores with no stock and no movement during the period',
            'default': True,
            'validator': bool,
        },
        'ALL_LOCATIONS': {
            'name': 'Always report the whole system',
            'description': 'Cover every store in the system (with an "All stores" total), no matter which location the report is printed from',
            'default': False,
            'validator': bool,
        },
    }

    def add_report_context(self, report_instance, model_instance, user, context):
        """Add report data to Stock Location reports."""
        from stock.models import StockLocation

        if not isinstance(model_instance, StockLocation):
            return

        # Stock Movement Summary - calculated lazily, only if the template uses it
        context['stock_movement'] = StockMovementReport(self, model_instance)

        # Project Stock Cost Report - totals for the stock at this location
        try:
            context.update(location_cost_totals(model_instance))
        except Exception:
            # Never break other reports - the template simply shows no totals
            pass

    def _int_setting(self, key, default, low, high):
        try:
            value = int(self.get_setting(key))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    def build_report(self, location):
        """Gather data from the database and compute the report."""
        from django.conf import settings
        from django.utils import timezone

        from common.currency import currency_code_default
        from djmoney.contrib.exchange.models import convert_money
        from stock.models import StockItem, StockItemTracking, StockLocation

        n_months = self._int_setting('REPORT_MONTHS', 12, 1, 60)
        max_depth = self._int_setting('MAX_DEPTH', 0, 0, 50)
        hide_empty = bool(self.get_setting('HIDE_EMPTY'))
        all_locations = bool(self.get_setting('ALL_LOCATIONS'))

        use_tz = getattr(settings, 'USE_TZ', False)
        now = timezone.now()

        def local(dt):
            if use_tz and timezone.is_aware(dt):
                return timezone.localtime(dt)
            return dt

        # Months, oldest first
        today = local(now)
        year, month = today.year, today.month
        keys = []
        for _ in range(n_months):
            keys.append((year, month))
            month -= 1
            if month == 0:
                month, year = 12, year - 1
        keys.reverse()

        months = []
        for y, m in keys:
            start = datetime(y, m, 1)
            if use_tz:
                start = timezone.make_aware(start)
            months.append(((y, m), start.strftime('%b %Y'), start))

        def month_key(dt):
            dt = local(dt)
            return (dt.year, dt.month)

        # Locations
        locations = {
            pk: {'parent': parent, 'name': name, 'path': path}
            for pk, parent, name, path in StockLocation.objects.values_list(
                'pk', 'parent_id', 'name', 'pathstring'
            )
        }

        if all_locations:
            roots = [pk for pk, info in locations.items() if info['parent'] is None]
            scope = 'All stores'
        else:
            roots = [location.pk]
            scope = location.pathstring or location.name

        # Stock items, with unit price in the default currency
        currency = currency_code_default()
        items = []
        for item in StockItem.objects.only(
            'pk', 'parent', 'location', 'quantity', 'purchase_price', 'purchase_price_currency'
        ).iterator(chunk_size=2000):
            price = None
            money = item.purchase_price
            if money is not None:
                try:
                    if str(money.currency) != str(currency):
                        money = convert_money(money, currency)
                    price = Decimal(money.amount)
                except Exception:
                    price = None
            items.append({
                'id': item.pk,
                'parent': item.parent_id,
                'location': item.location_id,
                'quantity': item.quantity,
                'price': price,
            })

        # Tracking history
        tracking = {}
        for item_id, date, code, deltas in (
            StockItemTracking.objects.filter(item__isnull=False, date__lte=now)
            .order_by('item_id', 'date', 'pk')
            .values_list('item_id', 'date', 'tracking_type', 'deltas')
            .iterator(chunk_size=5000)
        ):
            tracking.setdefault(item_id, []).append((date, code, deltas or {}))

        result = compute_movements(
            locations=locations,
            roots=roots,
            include_all_row=all_locations,
            items=items,
            tracking=tracking,
            months=months,
            month_key=month_key,
            now=now,
            max_depth=max_depth,
            hide_empty=hide_empty,
        )
        result['scope'] = scope
        result['currency'] = str(currency)
        return result
