"""Report observed strategy behavior without assigning causes to idle time."""
from __future__ import annotations

from collections import Counter
from typing import Any

TEAMS = ('red', 'blue')
NORMAL_RECEIPTS = {'ACCEPTED', 'BALL_REQUEST_ACCEPTED', 'CANCEL_PENDING', 'CANCELLED', 'COMPLETED'}


class MatchMetrics:
    """Accumulate frame sampled durations and exact event timestamps."""

    def __init__(self) -> None:
        self.teams = {
            team: {
                'qualification_ms': None, 'central_ball_placed_ms': None,
                'contested_receipts': 0, 'target_changed_receipts': 0,
                'other_rejection_receipts': 0, 'drop_events': 0,
                'br_recovery_completed_ms': [], 'tr_recovery_completed_ms': [],
                'empty_transfer_idle_sampled_ms': 0, 'supply_backpressure_sampled_ms': 0,
            }
            for team in TEAMS
        }
        self.receipt_ids: set[str] = set()

    def interval(self, observations: dict[str, Any], elapsed_ms: int) -> None:
        for team, record in self.teams.items():
            observation = observations[team]
            robot = observation['self']
            supply = observation['supply'][team]
            if (robot['pose'].get('workpoint_id') == team.upper() + '_TRANSFER'
                    and robot['control_state'] == 'READY'
                    and not robot['action'] and not robot['cargo']
                    and not any(supply['slots'].values())):
                record['empty_transfer_idle_sampled_ms'] += elapsed_ms
            if (supply.get('batch') or {}).get('status') == 'waiting_slots':
                record['supply_backpressure_sampled_ms'] += elapsed_ms

    def receipts(self, receipts: dict[str, list[dict[str, Any]]]) -> None:
        for team, rows in receipts.items():
            for row in rows:
                if row['id'] in self.receipt_ids:
                    continue
                self.receipt_ids.add(row['id'])
                code = row['code']
                if code == 'RESOURCE_CONTESTED':
                    self.teams[team]['contested_receipts'] += 1
                elif code == 'TARGET_CHANGED':
                    self.teams[team]['target_changed_receipts'] += 1
                elif code not in NORMAL_RECEIPTS:
                    self.teams[team]['other_rejection_receipts'] += 1

    def finish(self, events: list[dict[str, Any]], faults: list[dict[str, Any]]) -> dict[str, Any]:
        recovery: dict[tuple[str, str], int] = {}
        for event in events:
            team = event.get('team')
            if team not in self.teams:
                continue
            time = event['occurred_at_ms']
            kind = event['type']
            record = self.teams[team]
            if kind == 'QUALIFIED':
                record['qualification_ms'] = time
            elif kind == 'CENTRAL_BALL_PLACED':
                record['central_ball_placed_ms'] = time
            elif kind == 'ITEM_DROPPED':
                record['drop_events'] += 1
            for role, prefix in (('br', ''), ('tr', 'TR_')):
                if kind in (prefix + 'RETRY_STARTED', prefix + 'BOX_CLEARANCE_STARTED'):
                    recovery[team, role] = time
                elif kind == prefix + 'RETRY_READY' and (team, role) in recovery:
                    record[role + '_recovery_completed_ms'].append(time - recovery.pop((team, role)))
        return {
            'teams': self.teams,
            'faults_configured': len(faults),
            'faults_triggered': sum(fault.get('status') == 'triggered' for fault in faults),
        }


def series_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Map red and blue back to A/B and summarize normal and fault games separately."""
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, result in enumerate(results):
        group = 'fault' if result.get('metrics', {}).get('faults_configured') else 'normal'
        groups.setdefault(group, []).append((index, result))
    summary = {}
    for group, rows in groups.items():
        teams = {}
        for strategy in ('A', 'B'):
            totals: Counter[str] = Counter()
            qualification = []
            ball = []
            for index, result in rows:
                side = 'red' if (index % 2 == 0) == (strategy == 'A') else 'blue'
                totals['wins'] += result.get('winner') == side
                totals['draws'] += result.get('winner') is None
                totals['score'] += result.get('scores', {}).get(side, 0)
                metrics = result.get('metrics', {}).get('teams', {}).get(side, {})
                for key, values in (('qualification_ms', qualification), ('central_ball_placed_ms', ball)):
                    if metrics.get(key) is not None:
                        values.append(metrics[key])
                for key in ('contested_receipts', 'target_changed_receipts', 'drop_events'):
                    totals[key] += metrics.get(key, 0)
            teams[strategy] = {
                **totals, 'games': len(rows), 'average_score': totals['score'] / len(rows),
                'qualification_count': len(qualification), 'ball_placement_count': len(ball),
                'average_qualification_ms': sum(qualification) / len(qualification) if qualification else None,
                'average_ball_placed_ms': sum(ball) / len(ball) if ball else None,
            }
        summary[group] = {
            'teams': teams,
            'faults_configured': sum(result.get('metrics', {}).get('faults_configured', 0) for _, result in rows),
            'faults_triggered': sum(result.get('metrics', {}).get('faults_triggered', 0) for _, result in rows),
        }
    return summary
