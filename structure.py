"""Deterministic support/resistance zones for stop and target placement.

This module is pure math: it performs no I/O and never invents a level when
completed bars do not contain suitable structure.
"""

from __future__ import annotations

from dataclasses import dataclass

from broker import Bar


@dataclass(frozen=True)
class Zone:
    low: float
    high: float
    touches: int
    side: str  # "support" | "resistance"
    source: str

    @property
    def midpoint(self) -> float:
        return (self.low + self.high) / 2

    def describe(self) -> str:
        return (f"{self.low:.2f}-{self.high:.2f} "
                f"({self.side}, {self.source}, {self.touches} touch"
                f"{'es' if self.touches != 1 else ''})")


@dataclass(frozen=True)
class StopSelection:
    price: float
    zone: Zone
    distance: float


@dataclass(frozen=True)
class TargetSelection:
    price: float
    zone: Zone | None
    reward_risk: float
    source: str


def _pct(value: float) -> float:
    return value / 100.0


def _swing_indexes(bars: list[Bar], strength: int, use_highs: bool) -> list[int]:
    indexes = []
    for i in range(strength, len(bars) - strength):
        value = bars[i].high if use_highs else bars[i].low
        neighbors = [
            bars[i - offset].high if use_highs else bars[i - offset].low
            for offset in range(1, strength + 1)
        ] + [
            bars[i + offset].high if use_highs else bars[i + offset].low
            for offset in range(1, strength + 1)
        ]
        comparison = all(value > other for other in neighbors) if use_highs \
            else all(value < other for other in neighbors)
        if comparison:
            indexes.append(i)
    return indexes


def _cluster_levels(levels: list[float], tolerance: float, side: str) -> list[Zone]:
    """Cluster sorted swing prices whose range remains within tolerance."""
    if not levels:
        return []
    clusters: list[list[float]] = []
    for level in sorted(levels):
        if not clusters:
            clusters.append([level])
            continue
        center = sum(clusters[-1]) / len(clusters[-1])
        if center > 0 and abs(level - center) <= center * tolerance:
            clusters[-1].append(level)
        else:
            clusters.append([level])
    zones = []
    for cluster in clusters:
        touches = len(cluster)
        source = (
            ("equal_lows" if side == "support" else "equal_highs")
            if touches >= 2 else
            ("swing_low" if side == "support" else "swing_high")
        )
        zones.append(Zone(min(cluster), max(cluster), touches, side, source))
    return zones


def _merge_duplicate_zones(zones: list[Zone], tolerance: float) -> list[Zone]:
    """Prefer multi-touch zones when fixed session levels overlap swings."""
    ranked = sorted(
        zones,
        key=lambda zone: (-zone.touches, zone.high - zone.low, zone.midpoint),
    )
    kept: list[Zone] = []
    for zone in ranked:
        duplicate = any(
            zone.side == other.side
            and abs(zone.midpoint - other.midpoint)
            <= max(zone.midpoint, other.midpoint) * tolerance
            for other in kept
        )
        if not duplicate:
            kept.append(zone)
    return sorted(kept, key=lambda zone: zone.midpoint)


def detect_zones(bars_5m: list[Bar], cfg: dict,
                 previous_close: float | None = None) -> list[Zone]:
    """Build intraday support/resistance zones from completed 5-minute bars."""
    if not bars_5m:
        return []
    strength = max(1, int(cfg.get("swing_strength", 2)))
    tolerance = _pct(float(cfg.get("cluster_pct", 0.20)))
    low_indexes = _swing_indexes(bars_5m, strength, use_highs=False)
    high_indexes = _swing_indexes(bars_5m, strength, use_highs=True)
    zones = _cluster_levels(
        [bars_5m[index].low for index in low_indexes], tolerance, "support")
    zones += _cluster_levels(
        [bars_5m[index].high for index in high_indexes], tolerance, "resistance")

    session_low = min(bar.low for bar in bars_5m)
    session_high = max(bar.high for bar in bars_5m)
    zones.extend([
        Zone(session_low, session_low, 1, "support", "session_low"),
        Zone(session_high, session_high, 1, "resistance", "session_high"),
    ])

    # Three completed 5-minute bars form the opening 15-minute range.
    if len(bars_5m) >= 3:
        opening = bars_5m[:3]
        opening_low = min(bar.low for bar in opening)
        opening_high = max(bar.high for bar in opening)
        zones.extend([
            Zone(opening_low, opening_low, 1, "support", "opening_15m_low"),
            Zone(opening_high, opening_high, 1, "resistance", "opening_15m_high"),
        ])

    if previous_close is not None and previous_close > 0:
        latest = bars_5m[-1].close
        if previous_close < latest:
            zones.append(Zone(
                previous_close, previous_close, 1, "support", "previous_close"))
        elif previous_close > latest:
            zones.append(Zone(
                previous_close, previous_close, 1, "resistance", "previous_close"))

    return _merge_duplicate_zones(zones, tolerance)


def stop_for(entry: float, direction: str, structural_stop: float,
             zones: list[Zone], atr_5m: float | None,
             structure_cfg: dict, stops_cfg: dict,
             own_stop: bool = False) -> StopSelection | None:
    """Choose the nearest valid zone beyond the strategy's defining stop.

    With `own_stop`, the strategy's own level is treated as the structure and
    clamped into the risk band rather than searched past.
    """
    if entry <= 0 or direction not in ("long", "short"):
        return None
    atr_value = max(0.0, float(atr_5m or 0.0))
    buffer_distance = max(
        entry * _pct(float(structure_cfg.get("stop_buffer_pct", 0.10))),
        atr_value * float(structure_cfg.get("stop_buffer_atr_mult", 0.10)),
    )
    min_distance = max(
        atr_value * float(stops_cfg.get("atr_mult", 0.8)),
        entry * _pct(float(stops_cfg.get("min_stop_pct", 0.15))),
    )
    max_candidates = [
        entry * _pct(float(structure_cfg.get("max_stop_pct", 0.35))),
    ]
    if atr_value > 0:
        max_candidates.append(
            atr_value * float(structure_cfg.get("max_stop_atr_mult", 1.0)))
    # Both the volatility cap and percentage cap must pass. A wide ATR is not
    # permission to place an unreachable intraday stop.
    max_distance = min(max_candidates)

    # Some strategies mark their own structural level and place the stop just
    # beyond it — the opening scalp's stop sits a buffer past the 15-minute
    # opening-range high/low, which IS the structure being traded. Hunting for
    # a *different* zone beyond that stop and then demanding it also fall
    # inside max_distance rejects almost everything: measured over 435 cached
    # opening_scalp signals, 405 (93%) failed as "too far", because the next
    # real zone below the opening range is well past the cap.
    #
    # For those strategies, honour the strategy's own stop: clamp it into the
    # [min_distance, max_distance] risk band instead of discarding the trade.
    if own_stop:
        distance = abs(entry - structural_stop)
        clamped = min(max(distance, min_distance), max_distance)
        price = (entry - clamped if direction == "long" else entry + clamped)
        zone = Zone(low=price, high=price, touches=1,
                    side="support" if direction == "long" else "resistance",
                    source="strategy_level")
        return StopSelection(price, zone, clamped)

    candidates: list[StopSelection] = []
    for zone in zones:
        if direction == "long":
            if zone.side != "support" or zone.high >= entry:
                continue
            price = zone.low - buffer_distance
            if price > structural_stop:
                continue
        else:
            if zone.side != "resistance" or zone.low <= entry:
                continue
            price = zone.high + buffer_distance
            if price < structural_stop:
                continue
        distance = abs(entry - price)
        if min_distance <= distance <= max_distance:
            candidates.append(StopSelection(price, zone, distance))
    return min(candidates, key=lambda item: item.distance) if candidates else None


def target_for(entry: float, direction: str, stop: float,
               zones: list[Zone], structure_cfg: dict,
               orders_cfg: dict,
               atr_5m: float | None = None) -> TargetSelection | None:
    """Aim just before the nearest opposing zone while respecting the R band."""
    risk = abs(entry - stop)
    if risk <= 0 or direction not in ("long", "short"):
        return None
    min_rr = float(orders_cfg.get("min_reward_risk", 1.5))
    max_rr = float(orders_cfg.get("max_reward_risk", 2.5))
    fallback_rr = float(orders_cfg.get("target_rr", 2.0))
    front_run = _pct(float(structure_cfg.get("target_front_run_pct", 0.05)))
    max_reward_candidates = [
        max_rr * risk,
        entry * _pct(float(structure_cfg.get("max_target_pct", 0.50))),
    ]
    if atr_5m is not None and atr_5m > 0:
        max_reward_candidates.append(
            atr_5m * float(structure_cfg.get("max_target_atr_mult", 1.5)))
    max_reward = min(max_reward_candidates)
    if max_reward < min_rr * risk:
        return None

    candidates: list[tuple[float, Zone]] = []
    for zone in zones:
        if direction == "long" and zone.side == "resistance" and zone.low > entry:
            price = zone.low * (1 - front_run)
            candidates.append((price, zone))
        elif direction == "short" and zone.side == "support" and zone.high < entry:
            price = zone.high * (1 + front_run)
            candidates.append((price, zone))

    if candidates:
        price, zone = min(candidates, key=lambda item: abs(item[1].midpoint - entry))
        reward = price - entry if direction == "long" else entry - price
        if reward <= 0:
            return None
        rr = reward / risk
        if rr < min_rr:
            return None  # nearby structure blocks the required reward
        if reward > max_reward:
            sign = 1 if direction == "long" else -1
            price = entry + sign * max_reward
            rr = max_reward / risk
            source = "opposing_zone_intraday_capped"
        else:
            source = "opposing_zone"
        return TargetSelection(round(price, 2), zone, rr, source)

    fallback_rr = min(max(fallback_rr, min_rr), max_rr)
    fallback_reward = min(fallback_rr * risk, max_reward)
    if fallback_reward < min_rr * risk:
        return None
    sign = 1 if direction == "long" else -1
    price = entry + sign * fallback_reward
    return TargetSelection(
        round(price, 2), None, fallback_reward / risk,
        "no_opposing_zone_intraday_fallback")
