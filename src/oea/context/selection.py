"""Deterministic bounded multiple-choice selection; no model or tool calls."""

from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True, slots=True)
class Group:
    full: int
    reference: int | None = None
    protected: bool = False


@dataclass(frozen=True, slots=True)
class Selection:
    modes: tuple[str, ...]
    summarized: int
    cost: int
    utility: int
    quantum: int
    capacity: int
    fallback: bool = False


def utility(index, count, mode, recent_turns):
    weight = index + 1 + (count if index >= count - recent_turns else 0)
    return weight * (4 if mode == "full" else 1)


def _plan(groups, budget, recent_turns, width, allow_summary):
    quantum = max(1, ceil(budget / width))
    capacity = budget // quantum
    scores, costs = [-1] * (capacity + 1), [0] * (capacity + 1)
    trace, prefix_allowed = [], True
    for index, group in enumerate(groups):
        next_scores, next_costs = [-1] * (capacity + 1), [0] * (capacity + 1)
        choices = bytearray(capacity + 1)
        options = [(1, group.full, utility(index, len(groups), "full", recent_turns))]
        if group.reference is not None and group.reference < group.full:
            options.append((2, group.reference, utility(index, len(groups), "reference", recent_turns)))
        for mode, cost, score in options:
            units = (cost + quantum - 1) // quantum
            if prefix_allowed and units <= capacity:
                if score > next_scores[units] or score == next_scores[units] and cost < next_costs[units]:
                    next_scores[units], next_costs[units], choices[units] = score, cost, mode + 2
            for before in range(capacity - units + 1):
                if scores[before] < 0:
                    continue
                after, value, actual = before + units, scores[before] + score, costs[before] + cost
                if value > next_scores[after] or value == next_scores[after] and actual < next_costs[after]:
                    next_scores[after], next_costs[after], choices[after] = value, actual, mode
        trace.append(choices)
        scores, costs = next_scores, next_costs
        prefix_allowed = prefix_allowed and allow_summary and not group.protected and index != len(groups) - 1
    feasible = [index for index, score in enumerate(scores) if score >= 0]
    if not feasible:
        return None
    position = max(feasible, key=lambda index: (scores[index], -costs[index], -index))
    score, cost = scores[position], costs[position]
    modes, prefix = ["summary"] * len(groups), 0
    for index in range(len(groups) - 1, -1, -1):
        code = trace[index][position]
        mode = "full" if code in {1, 3} else "reference"
        modes[index] = mode
        if code > 2:
            prefix = index
            break
        raw = groups[index].full if mode == "full" else groups[index].reference
        position -= (raw + quantum - 1) // quantum
    return Selection(tuple(modes), prefix, cost, score, quantum, capacity)


def select_views(groups, budget, *, recent_turns=12, allow_summary=False):
    """Optimize only this bounded candidate set under the declared local utility.

    Quantized costs round up and capacity rounds down. Exact minimum views are
    checked before a quantization failure can cause compaction or rejection.
    """
    if budget < 0:
        return None
    groups = tuple(groups)
    if not groups:
        return Selection((), 0, 0, 0, 1, 0)
    full = sum(group.full for group in groups)
    if full <= budget:
        return Selection(("full",) * len(groups), 0, full,
                         sum(utility(i, len(groups), "full", recent_turns) for i in range(len(groups))), 1, 0)
    minimum = [("reference", group.reference) if group.reference is not None and group.reference < group.full
               else ("full", group.full) for group in groups]
    total = sum(cost for _, cost in minimum)
    # Full retention always has priority over summarizing any prefix.
    summary = allow_summary and total > budget
    if total > budget and not summary:
        return None
    for width in (2048, 4096):
        chosen = _plan(groups, budget, recent_turns, width, summary)
        if chosen is not None:
            return chosen
        if budget <= width:
            break
    prefix = 0
    if total > budget:
        while total > budget and prefix < len(groups) - 1 and not groups[prefix].protected:
            total -= minimum[prefix][1]
            prefix += 1
    if total > budget:
        return None
    modes = ("summary",) * prefix + tuple(mode for mode, _ in minimum[prefix:])
    return Selection(modes, prefix, total,
                     sum(utility(i, len(groups), mode, recent_turns) for i, mode in enumerate(modes) if mode != "summary"),
                     max(1, ceil(budget / 4096)), budget // max(1, ceil(budget / 4096)), True)
