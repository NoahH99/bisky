# Aura: economic specification

Technical reference for Bisky's economy. Every figure here is derived from the
constants in `src/bisky/economy.py` and `src/bisky/games.py`, and the ones that
can be checked mechanically are asserted in `tests/test_economy_rules.py` and
`tests/test_games.py`.

Notation: `⌊x⌋` is floor. Money is always integer aura. `E[·]` is expectation
per unit staked, so `RTP = E[payout]/stake` and `edge = 1 − RTP`.

---

## 1. Invariants

**I1 — Integer money.** Every balance, price, fee and payout is an integer.
Floating-point currency accumulates representation error that surfaces as aura
appearing or vanishing. All percentage arithmetic is `⌊a·p/100⌋`.

**I2 — Non-negative balances.** `wallet ≥ 0` and `bank ≥ 0` are `CHECK`
constraints, and every debit is a conditional `UPDATE` (§8).

**I3 — Conservation.** Aura enters only through §2 sources and leaves only
through §4 sinks. Transfers between players conserve total supply minus their
tax. A failed operation must move nothing.

**I4 — Winnings are not income.** Gambling payouts, transfers received and
robbery proceeds are credited with `count_as_earned=False`. Only §2 sources
increment `lifetime_earned`, which is what the pacing model in §3 is built on.

---

## 2. Sources

| Source | Rate | Cap |
| --- | --- | --- |
| Voice | `voice_aura_per_minute = 1` per qualifying minute | none |
| Message | `message_aura = 2` per message | one per `message_cooldown_seconds = 60` |
| Work | `U{100, …, 200}` (uniform) | one per `work_cooldown_seconds = 86400` |
| Admin grant | arbitrary, via `!economy give` | global admins only |

Administrative grants are real minting and are counted as
`bisky_economy_minted_total{source="admin"}`, so hand-outs show up in the
inflation graph rather than quietly invalidating the model in §4.1. They do not
increment `lifetime_earned` (I4): the aura was not earned, and counting it would
distort the progression figures in §3.

Daily income for a member with `h` qualifying voice hours and `m` rewarded
messages:

```
I(h, m) = 60h + 2m + E[work]        E[work] = (100 + 200)/2 = 150
```

### 2.1 Voice eligibility

A member accrues on a tick iff all three hold:

```
humans_in_channel ≥ min_voice_humans (= 2)     ∧
¬self_deafened                                  ∧
channel ≠ guild.afk_channel
```

`humans_in_channel` excludes bots, so sitting alone with Bisky is still alone.

**Why these are load-bearing.** Without them, overnight AFK earns `8 × 60 =
480`/day = **175,200/year**, which clears cumulative tiers 1–5 in a single year
of sleeping. A 24/7 idle client earns **525,600/year** and clears tier 6. The
guards are the difference between an economy and a faucet.

### 2.2 A known asymmetry

Per *active minute*, chat pays `2` and voice pays `1` — chat is nominally worth
**2× voice**. This is intentional only insofar as voice is passive: you can earn
voice aura while doing something else, whereas a message every 60 seconds for
three hours is real effort. If chat income ever dominates in practice, the lever
is `message_aura → 1` or `message_cooldown_seconds → 120`.

Theoretical daily maximum from chat alone is `2 × 1440 = 2880`, which is 5.6×
the modelled 6 h/day figure. Nobody will do this, but it is the ceiling.

---

## 3. Progression model

### 3.1 The ladder

Default prices `p₁…p₇`, cumulative `C_k = Σᵢ₌₁ᵏ pᵢ`:

| Tier | Price `p_k` | Cumulative `C_k` | `p_k/p_{k−1}` | `C_k/C_{k−1}` |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 250 | 250 | — | — |
| 2 | 1,000 | 1,250 | 4.00 | 5.00 |
| 3 | 4,000 | 5,250 | 4.00 | 4.20 |
| 4 | 15,000 | 20,250 | 3.75 | 3.86 |
| 5 | 55,000 | 75,250 | 3.67 | 3.72 |
| 6 | 175,000 | 250,250 | 3.18 | 3.33 |
| 7 | 500,000 | 750,250 | 2.86 | 3.00 |

The design property is the **last column**: each tier costs about three times
the *cumulative* effort of everything before it. That makes the unit of progress
shift — hours, then days, then weeks, then months, then years — which is what
produces a long chase that still feels like it is moving early on.

The per-tier ratio deliberately tapers from 4.0 to 2.86 so the final step is a
long grind without being absurd. `test_ladder_grows_geometrically` bounds it to
`[2.5, 5.0]`.

### 3.2 Time to complete

Days to tier `k` at daily income `I`: `t_k = C_k / I`.

| Voice/day | Work | `I` | Tier 7 |
| ---: | ---: | ---: | ---: |
| 0 h | ✓ | 150 | 13.70 yr |
| 1 h | ✗ | 60 | **34.26 yr** |
| 1 h | ✓ | 210 | 9.79 yr |
| 3 h | ✗ | 180 | 11.42 yr |
| 3 h | ✓ | 330 | 6.23 yr |
| 6 h | ✗ | 360 | 5.71 yr |
| 6 h | ✓ | 510 | **4.03 yr** |
| 12 h | ✓ | 870 | 2.36 yr |

Per-tier, at the modelled `I = 510`:

| Tier | Days | Human scale |
| ---: | ---: | --- |
| 1 | 0.5 | an evening |
| 2 | 2.5 | a few days |
| 3 | 10.3 | a week and a half |
| 4 | 39.7 | ~6 weeks |
| 5 | 147.5 | ~5 months |
| 6 | 490.7 | ~1.3 years |
| 7 | 1,471 | ~4 years |

### 3.3 Why `!work` exists

Look at the `1 h, ✗` row: **34 years**. Priced against voice alone, the ladder
is completable only by the single most active member of the server. `!work` is
not a bonus, it is the accessibility floor — it takes the 1 h/day case from
34 years to 9.8, while a 6 h/day member still finishes 2.4× faster.

The gap between heaviest and lightest realistic user compresses from 6.0× to
2.4×, which is a deliberate trade: some flattening of the voice reward in
exchange for the ladder being reachable at all.

---

## 4. Sinks

| Sink | Rate | Burns or moves |
| --- | --- | --- |
| Role purchase | full price | burns |
| Deposit fee | `⌊0.05·a⌋` | burns |
| Gambling edge | 2.5 % – 6.94 % of volume | burns |
| Lottery rake | 10 % of pot | burns |
| Duel rake | 5 % of pot | burns |
| Robbery fine | `⌊0.10·W_robber⌋` on failure | burns |
| Transfer tax | `⌊0.05·a⌋` | burns |
| Robbery take | 25 % of victim's wallet | **moves** |
| Transfer body | remainder | **moves** |
| Admin removal | arbitrary, via `!economy take` / `reset` | burns |

### 4.1 Steady-state capacity

Supply obeys `dM/dt = sources − sinks`. The ladder is a *one-off* sink of
750,250 per player; once exhausted only the recurring sinks remain. To hold a
player at zero net inflation on `I = 510`/day, wagering volume `V` must satisfy
`e·V = I`:

| Game | Edge `e` | `V` needed | as ×income |
| --- | ---: | ---: | ---: |
| Coinflip | 0.0250 | 20,400/day | 40.0× |
| Roulette | 0.0270 | 18,870/day | 37.0× |
| Dice | 0.0300 | 17,000/day | 33.3× |
| Slots | 0.0694 | 7,344/day | 14.4× |

The deposit fee, by contrast, can absorb **at most 5 % of income** (`25.5`/day)
even if a player banks every single aura.

**Conclusion, stated plainly:** the recurring sinks cannot absorb ordinary
income unless players gamble 14–40× their earnings. Over the ~4-year ladder the
economy is near-balanced because the ladder dominates; *after* it, supply grows
roughly linearly at `I` minus whatever gambling and lottery activity happens to
occur. The lottery is the only sink that recurs indefinitely, which is why it
exists, but it is not sufficient on its own. This is the main open design
problem — see §9.

---

## 5. Game mathematics

All games take an injected `random.Random`. Where the outcome space is finite
and small, `tests/test_games.py` enumerates it exhaustively rather than
sampling.

Let `X` be the payout multiple (stake included, so `X = 0` on a total loss).
`RTP = E[X]`, `edge = 1 − E[X]`, `σ = √Var(X)` per unit staked.

### 5.1 Coinflip

```
Ω = {heads, tails},  P(win) = 1/2,  payout = ⌊stake · 19500/10000⌋ = 1.95·stake

E[X]   = ½ · 1.95 = 39/40 = 0.975            edge = 2.50 %
E[X²]  = ½ · 1.95² = 1.901250
Var(X) = 0.950625                             σ = 0.9750
```

### 5.2 Slots

Three reels, six equiprobable symbols, `|Ω| = 6³ = 216`.

```
triples          6 outcomes   P = 1/36  ≈ 0.027778   pays 11×
exactly a pair  90 outcomes   P = 5/12  ≈ 0.416667   pays 1.5×
all distinct   120 outcomes   P = 5/9   ≈ 0.555556   pays 0
                216 ✓
```

Counting: pairs are `C(3,2) · 6 · 5 = 90` (choose the two matching positions,
the pair symbol, then a different symbol); distinct is `6·5·4 = 120`.

```
E[X]   = (6·11 + 90·1.5)/216 = 201/216 = 67/72 = 0.930556   edge = 6.94 %
E[X²]  = (6·121 + 90·2.25)/216 = 619/144 = 4.298611
Var(X) = 4.298611 − 0.865934 = 3.432677          σ = 1.8527
```

Slots carry the largest edge *and* the largest variance of the even-money-ish
games, which is the classic slot-machine shape: mostly small losses punctuated
by a rare 11×.

### 5.3 Roulette

European single-zero wheel, `|Ω| = 37` pockets `{0, …, 36}`, 18 red, 18 black.

**Zero loses every outside bet.** That single pocket is the entire house edge:

```
outside (red/black/even/odd):  P = 18/37,  pays 2×
  E[X] = 36/37 = 0.972973      edge = 1/37 = 2.7027 %
  Var  = (18/37)·4 − (36/37)² = 0.999270        σ = 0.9996

straight (single number):      P = 1/37,   pays 36×
  E[X] = 36/37 = 0.972973      edge = 1/37 = 2.7027 %
  Var  = (1/37)·1296 − (36/37)² = 34.079        σ = 5.8378
```

Identical RTP, variance differing by **34×**. Betting a number is not a worse
deal, only a louder one.

Note `even` excludes pocket 0 despite 0 being numerically even — the
implementation short-circuits every outside bet on zero.

### 5.4 Dice

Roll `R ~ U{1, …, 100}`. The player picks a direction and a target
`t ∈ [1, 99]`:

```
over t:   win ⟺ R > t,  win_percent p = 100 − t
under t:  win ⟺ R < t,  win_percent p = t − 1
```

Payout multiplier is `m(p) = ⌊stake · 9700 / (100p)⌋ / stake ≈ 97/p`. Therefore

```
E[X] = (p/100) · (97/p) = 97/100 = 0.97   for every p          edge = 3.00 %
```

The `p` cancels — **the edge is exactly flat across every target the player can
pick**. Choosing longer odds buys variance, not value:

| `p` | multiplier | RTP | `σ` |
| ---: | ---: | ---: | ---: |
| 10 % | 9.700× | 0.9700 | 2.9100 |
| 25 % | 3.880× | 0.9700 | 1.6801 |
| 50 % | 1.940× | 0.9700 | 0.9700 |
| 75 % | 1.293× | 0.9700 | 0.5600 |
| 90 % | 1.078× | 0.9700 | 0.3233 |

`Var(X) = 9409/(100p) − 0.9409`, so `σ ∝ p^(−1/2)`.

Integer flooring makes realised RTP very slightly *below* 0.97 at high `p`
(measured 0.9699 at `p = 90`); the loss is at most one aura per wager and always
favours the house.

### 5.5 Robbery

```
P(success) = 0.35
take       = max(1, ⌊0.25 · W_victim⌋)        transferred, not created
fine       = min(W_robber, max(1, ⌊0.10 · W_robber⌋))   burned, on failure
guards     : cooldown 4 h; victim wallet ≥ 100 or the cooldown is refunded
```

Expected value to the robber:

```
EV = 0.35 · 0.25·W_v − 0.65 · 0.10·W_r = 0.0875·W_v − 0.065·W_r
```

Break-even at `W_v = 0.743 · W_r`. **Robbing is only +EV when the target is
carrying more than about 74 % of what you are carrying** — so the rational play
is to bank your own aura and hunt people who have not. That is exactly the
tension the wallet/bank split is meant to create.

Expected burn per attempt is `0.065 · W_r`.

Without the fine, `EV = 0.0875·W_v > 0` unconditionally: robbery would be a free
roll and the optimal strategy would be to spam it at everyone forever.

### 5.6 Duel

Both players stake `s`; pot is `2s`; rake is `⌊0.05 · 2s⌋ = 0.1s`; winner
receives `1.9s`.

```
EV per player = ½ · 1.9s − s = −0.05s          edge = 5 % of each stake
burn per duel = 0.1s = 5 % of total wagered
```

If the opponent cannot cover the stake the challenger is refunded — they never
consented to the wager.

### 5.7 Lottery

Tickets cost `c`; a player holds `k` of `n` total; pot `P = n·c`; rake 10 %.

```
EV = (k/n) · 0.9 · nc − kc = 0.9kc − kc = −0.1kc          edge = 10 %
```

The `n` cancels: **buying more tickets scales stake and expected return
identically**, so there is no volume discount and no advantage to sniping a
large pot late. Winner selection is ticket-weighted:

```
P(user i wins) = kᵢ / Σⱼ kⱼ
```

implemented as a linear scan over a single `randrange(Σk)` draw. An empty draw
returns `None` and the pot rolls over.

### 5.8 Transfers

```
received = a − ⌊0.05a⌋       burn = ⌊0.05a⌋
```

The tax exists so `!pay` is not a free way to empty a wallet the instant someone
threatens to rob it — otherwise the dominant strategy is to park everything with
a friend rather than pay the 5 % deposit fee.

---

## 6. Rounding

All percentage arithmetic floors, which rounds **in the player's favour**. The
consequence:

```
⌊0.05a⌋ = 0  for all a ≤ 19
```

So **deposits and transfers of 19 aura or less are free**. Banking 19 aura a
thousand times moves 19,000 with zero fee, where a single 19,000 deposit costs
950.

This is a real hole, mitigated only by it requiring a thousand manual commands.
If it ever matters, the fixes in increasing severity are: a minimum deposit of
20; `max(1, ⌊0.05a⌋)`; or ceiling the fee. Ceiling is not recommended — it makes
a 1-aura deposit cost 1 aura.

---

## 7. Configuration

Every rate below is a per-guild row in `economy_settings`, tunable live with
`!economy set <field> <value>`. Changing prices does **not** require a deploy.

| Field | Default | Floor | Effect |
| --- | ---: | ---: | --- |
| `voice_aura_per_minute` | 1 | 0 | linear on all voice income |
| `message_aura` | 2 | 0 | linear on all chat income |
| `message_cooldown_seconds` | 60 | 0 | inverse on chat income |
| `deposit_fee_percent` | 5 | 0 | the universal sink |
| `min_voice_humans` | 2 | 1 | idle protection |
| `work_min` / `work_max` | 100 / 200 | 0 | the accessibility floor |
| `work_cooldown_seconds` | 86400 | 0 | inverse on work income |
| `min_bet` / `max_bet` | 10 / 0 | 1 / 0 | `max_bet = 0` means unlimited |
| `lottery_ticket_price` | 100 | 1 | pot growth rate |

Role prices are rows in `economy_role_rewards`, so the whole ladder is
re-pricable without touching code.

**If you change income, re-derive §3.2.** `t_7 = C_7 / I`, so halving income
doubles every completion time. The pacing bounds in `test_economy_rules.py` will
fail if the *default ladder* drifts, but they cannot see per-guild overrides.

---

## 8. Concurrency and integrity

Every debit is expressed as a conditional `UPDATE`, never a read-modify-write:

```sql
UPDATE economy_accounts
   SET wallet = wallet - :amount
 WHERE guild_id = :g AND user_id = :u AND wallet >= :amount
```

Success is `rowcount == 1`. Two commands racing on the same wallet would
otherwise both read the pre-debit balance and both succeed, spending the same
aura twice. Putting the guard in the `WHERE` clause makes the database the
arbiter, needs no explicit locking, and behaves identically on PostgreSQL and on
the SQLite used by tests.

The same pattern claims a persisted cooldown:

```sql
UPDATE economy_cooldowns SET used_at = :now
 WHERE guild_id = :g AND user_id = :u AND key = :k AND used_at <= :cutoff
```

Tested by `test_concurrent_debits_cannot_double_spend` and
`test_concurrent_claims_only_let_one_through`, which each fire two simultaneous
operations and assert exactly one wins.

**Cooldown placement** follows one rule — *is the cooldown longer than the gap
between restarts?*

| Cooldown | Storage | Rationale |
| --- | --- | --- |
| Message (60 s) | memory | A restart costs 2 aura; not worth a read per message. |
| Work (24 h) | `economy_cooldowns` | In memory this would grant a free claim on every deploy. |
| Rob (4 h) | `economy_cooldowns` | Same. |

---

## 9. Observability and open problems

Exported counters:

```
bisky_economy_minted_total{source}   source ∈ {voice, message, work, gambling}
bisky_economy_burned_total{sink}     sink   ∈ {deposit_fee, role_purchase, gambling,
                                               rob_fine, duel_rake, lottery_rake,
                                               transfer_tax}
bisky_economy_voice_earners
```

Net inflation is `rate(minted) − rate(burned)`. Retune from that graph rather
than from intuition.

**Open problems, honestly stated:**

1. **Post-ladder inflation.** §4.1 shows recurring sinks need 14–40× income in
   wagering volume to break even. Once a player owns tier 7 their balance grows
   without bound. The lottery mitigates but does not solve this; repeatable
   sinks with genuine demand (temporary perks, consumables, recurring cosmetic
   purchases) are the real answer and are deliberately not guessed at yet.
2. **Chat/voice asymmetry** (§2.2) — chat pays 2× voice per active minute.
3. **Sub-20 rounding** (§6).
4. **No global aura.** Balances are per-guild by design; there is no
   cross-server economy and no migration path if that is ever wanted.
5. **Role reconciliation.** A purchased role removed manually in Discord is not
   re-granted. The purchase is recorded, so a reconcile-on-join job is possible
   but does not exist.
