# Eval report — 2026-07-18T09:58:45.909693Z

- Dataset: `/Users/lans_h/Documents/claude/snoopy_home/evals/dataset/golden.jsonl` (55 cases)
- Deterministic pass rate: **54/55 (98.2%)** (threshold 90%)
- Judge mean score: **4.65 / 5** (55 judged)

## By tag

| tag | passed | total |
|---|---|---|
| advice | 3 | 3 |
| cancel_reminder | 2 | 2 |
| chitchat | 3 | 3 |
| complete_chore | 3 | 3 |
| create_calendar_event | 4 | 4 |
| create_chore | 4 | 4 |
| create_reminder | 17 | 17 |
| delete_calendar_event | 2 | 2 |
| edge | 2 | 2 |
| group | 3 | 3 |
| no_action | 13 | 14 |
| query | 4 | 5 |
| recurring | 4 | 4 |
| relative_time | 3 | 3 |
| relay | 2 | 2 |
| remind_vs_relay | 2 | 2 |
| speak_in_voice | 2 | 2 |
| update_calendar_event | 3 | 3 |
| update_profile | 4 | 4 |
| voice | 2 | 2 |

## Failures

### query-gen-02
- message: `when is the next dentist appointment?`
- ✗ action types: got ['get_calendar_events'], expected []
- reply: Checking the calendar...


## Low judge scores (≤2)

- **chore-add-03** (1/5): The bot's reply satisfies the rubric by confirming the chore for Alice with an every-2-days schedule. It is also concise and warm. However, it critically fails the 'no invented facts' general expectation by adding 'at 9 AM' to the schedule, which was not specified by the user. This makes the confirmation misleading, as it presents an unrequested detail as part of the confirmed task, which is a significant flaw for a task management bot.
- **advice-01** (1/5): The bot failed to provide the expected frequency for deep cleaning the oven, suggesting quarterly instead of the rubric's specified "roughly monthly". Additionally, it "created" extra information by listing other deep cleaning tasks, which goes against the rubric's instruction to not create anything.
- **advice-03** (2/5): The bot proposes a concrete rotation for both tasks and does not create new chores, satisfying the explicit rubric. However, the proposed dish rotation (Alice: 3 days, Bob: 4 days) is not fair, despite the bot explicitly stating "To make it fair" and "balances out nicely." This makes the bot's claim misleading and fails to address the user's core request for a *fair* rotation for dishes. The vacuuming rotation is fair. The "*wags tail*" is a minor detraction from the "warm but not sycophantic" expectation. The significant failure in fairness for one of the tasks, combined with the misleading claim, prevents a higher score.
