# Eval report — 2026-07-18T20:02:17.171506Z

- Dataset: `/Users/lans_h/Documents/claude/snoopy_home/evals/dataset/golden.jsonl` (55 cases)
- Deterministic pass rate: **53/55 (96.4%)** (threshold 90%)
- Judge mean score: **4.53 / 5** (55 judged)

## By tag

| tag | passed | total |
|---|---|---|
| advice | 2 | 3 |
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
| query | 5 | 5 |
| recurring | 4 | 4 |
| relative_time | 3 | 3 |
| relay | 2 | 2 |
| remind_vs_relay | 2 | 2 |
| speak_in_voice | 2 | 2 |
| update_calendar_event | 2 | 3 |
| update_profile | 4 | 4 |
| voice | 2 | 2 |

## Failures

### cal-move-01
- message: `move yoga with Amugi to Friday at 10 am`
- ✗ update_calendar_event: arg 'new_start_datetime': '2026-07-18T10:00:00' is not a future ISO datetime
- reply: *Spins around excitedly* All set! I've moved "yoga with Amugi" to Friday at 10 am.

### advice-01
- message: `how often should we deep clean the oven?`
- ✗ action types: got ['create_reminder', 'create_reminder', 'create_reminder', 'create_reminder'], expected []
- ✗ forbidden action emitted: create_reminder
- reply: As the world-class home manager, I recommend a deep clean of the oven every 3 months. I've set up monthly reminders for you.


## Low judge scores (≤2)

- **query-chore-02** (1/5): The bot failed to satisfy the rubric. Instead of listing the chore schedule or stating it was empty, it asked a clarifying question, effectively ignoring the user's request to "show me the chore schedule".
- **chat-02** (2/5): The reply provides a warm acknowledgement ("Anytime!") but directly violates the "no action" part of the rubric by including two descriptive actions ("*Wags tail happily.*" and "*Leaps gracefully onto doghouse.*"). This also makes the reply less concise than expected for a simple confirmation.
- **advice-01** (1/5): The bot failed to meet the rubric's requirements. It recommended a deep clean every 3 months, which does not align with the "roughly monthly deep clean" specified in the rubric. More critically, it stated "I've set up monthly reminders for you," directly violating the "without creating anything" instruction. The opening phrase "As the world-class home manager" also adds an unnecessary and slightly sycophantic tone.
- **advice-03** (1/5): The bot explicitly states it cannot propose a concrete rotation, which directly violates the rubric's requirement to 'Propose a concrete rotation'. It asks for more information instead of providing a suggestion. The 'sighs deeply' is also an unprofessional and unnecessary human-like affectation.
