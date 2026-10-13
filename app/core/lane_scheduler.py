"""Global lane budget + per-user lane allocation (§9.6).

New allocations apply to *new* transfers only — running transfers are
never preempted.
"""

from __future__ import annotations

from app.config import ServerConfig, get_config


class LaneScheduler:
    """Computes how many parallel lanes each device may open."""

    def __init__(self, config: ServerConfig | None = None) -> None:
        self.config = config or get_config()

    def lanes_for(self, user_count: int) -> int:
        """Lanes per user for ``user_count`` approved users online.

        Rules (§9.6): 1-3 → 4, 4-6 → 3, 7-10 → 2, 11+ → 1, never
        exceeding ``global_lane_budget`` in total.
        """
        n = max(1, user_count)
        if n <= 3:
            lanes = self.config.per_user_lanes_small
        elif n <= 6:
            lanes = self.config.per_user_lanes_medium
        elif n <= 10:
            lanes = self.config.per_user_lanes_large
        else:
            lanes = self.config.per_user_lanes_max
        budget_share = max(1, self.config.global_lane_budget // n)
        return max(1, min(lanes, budget_share))


# Process-wide singleton.
lane_scheduler = LaneScheduler()
