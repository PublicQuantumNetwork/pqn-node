# Plan: Operation mode (entanglement / correlation)

> **This is the living tracker for this project.** Every working session (human or agent) reads it first and updates it before finishing:
> 1. Tick the checkboxes in [Phases](#phases) as actions are completed.
> 2. Add a row to the [Work log](#work-log) (newest first): date, who, what was done, and anything that went wrong or was decided.
> 3. If a decision changes, update [Design decisions](#design-decisions) and the matching ADR, then log the change. Don't silently rewrite history.
>
> Related: [`CONTEXT.md`](../CONTEXT.md) (glossary) · [`.agents/`](../.agents/) (agent [Workflow](#workflow))

## Status

| Phase | State | Branch / worktree / PR |
| --- | --- | --- |
| 0. Design and workflow | Done (2026-10-27), except installing Node.js for pqn-gui (manual) | `marcosfrenkel/low_entanglement_mode` · `pqn-node/low_entanglement_mode` |
| 1. Settings and Whobot controls | Done (2026-10-27) | `marcosfrenkel/operation_mode_settings` · `pqn-node/operation_mode_settings` |
| 2. Correlation-mode SSM | Done (2026-10-27) | `marcosfrenkel/correlation_ssm` · `pqn-node/correlation_ssm` |
| 3. Unattended SSM and the digest | Done (2026-10-27) | `marcosfrenkel/unattended_ssm` · `pqn-node/unattended_ssm` |
| 4. pqn-gui | Not started | `marcosfrenkel/operation_mode` · `pqn-gui/operation_mode` |
| 5. Deploy and lab check | Not started | — |

Worktrees are under `/home/marcosf2/orca/workspaces/`. Each pqn-node branch is based on the previous phase's branch.

## Summary

When the photon source is giving correlated but barely entangled pairs, the Node can switch its **Operation mode** from `entanglement` to `correlation`. In correlation mode, Share a Secret Message (SSM) becomes a game where each player's Answer moves only their own waveplate (left → V, right → D), and the coincidence counts reveal whether the two players agreed. CHSH and Quantum Fortune play as before; the GUI explains that entanglement is low. The mode, the Calibrated angles for H/V/D/A and the Reference rates live in `config.toml`, and Whobot can change all of them. Whobot also gains an unattended SSM check, on demand and in the daily digest, which in entanglement mode compares the two Nodes' key bits for the first time.

The work ships as three stacked pqn-node PRs (Phases 1–3) on top of a design PR (Phase 0), plus a pqn-gui PR (Phase 4), each in its own Orca worktree.

## Open questions

None. All were resolved in the design session (2026-10-26/27) and recorded in [Design decisions](#design-decisions).

## User-facing text

Shown by pqn-gui only when the Node's Operation mode is `correlation` (decision 18).

| Where | Text |
| --- | --- |
| Home screen | "Entanglement is low today. For two-player games, we will use a mode that does not rely on entanglement." |
| CHSH start screen | "Entanglement is low today, so your link may not beat the classical limit." |
| SSM start screen | "Entanglement is low today, so this game uses photons that are correlated instead of entangled. [Learn more](https://publicquantumnetwork.org)" |
| SSM result page | Keeps "Matching bits: X / Y" and the blurred emoji, and adds: "Today the photons told us how many answers you agreed on, but they couldn't keep a secret." |

## Design decisions

| # | Decision | Why | Rejected alternatives |
| --- | --- | --- | --- |
| 1 | In correlation-mode SSM each player's Answer sets only their own waveplate: left → V, right → D (library D = halfway between its calibrated max and min). Expected levels with the ≈ VV product source: VV high (1), VD/DV medium (½), DD low (¼). High or low → agree, medium → disagree | The photons actually reveal agreement; neither Node needs the other's Answer before measuring; fits today's code where each Node moves only its own waveplate | Joint settings from the whiteboard (VV / HH / VD), which give cleaner levels (≈3K / ≈100) but mean the Node computes agreement before measuring |
| 2 | The waveplate angles and Coincidence level references for correlation-mode SSM live in `config.toml` and Whobot can change them. Nothing is measured before an SSM game (amended 2026-10-27; it first read "measured in a calibration") | A calibration before every game makes each game slower; operators set the values by hand and Whobot can update them | Calibrating before every game; caching a calibration for N minutes |
| 3 | correlation-mode SSM shares no secret: it only reveals, through the photons, how often the two players agreed | No entanglement means no correlated key bits in the D/A basis | — |
| 4 | Entangled-mode SSM keeps its logic exactly as it is (amended 2026-10-27 by decision 6: it reads the calibrated H/V/D/A angles, which default to today's values) | Out of scope; the result there is the count of matching Answers, and the key bits (which do depend on the counts) are never compared or shown | Making it compare the leader's and follower's key bits |
| 5 | The waveplate angles for H, V, D and A are four independent values in the Node's settings, app-wide (not under `qkd_settings`), not derived from one offset. Defaults equal today's `QKDAngleValuesHWP` values | Calibrates each named polarization separately; matches how the setup is used (one rotator per Node in practice) | One offset per device (H/V/D/A rigidly 22.5° apart, also covers CHSH's continuous angles) |
| 6 | The calibrated H/V/D/A angles are used by every move to a named polarization: SSM in both modes, and the Angle Sweep's follower pin when it is H/V/D/A. Continuous angles (CHSH, the sweep's leader steps, a custom follower angle) keep moving raw (`polarization / 2`). Whobot's sweep plot labels stay nominal | Four points can't map continuous angles; named moves are exactly where calibration matters | Calibrating CHSH too (would need an offset model, rejected in decision 5) |
| 7 | Each Node holds its own Coincidence level references, used only when it leads: three reference rates (coincidences per second) for VV, VD and DD. A question's count, scaled by the integration time used, is classified by the nearest reference | No coordination between Nodes; references are what an operator measures; rates survive integration-time changes | The leader fetching the follower's values; two explicit thresholds; raw counts |
| 8 | The Operation mode is a top-level config key `operation_mode`, values `"entanglement"` (default, today's behaviour) and `"correlation"`; a `StrEnum` `OperationMode` in `constants.py`; same strings in the API and Whobot. Correlation-mode values go in a `[correlation_settings]` table (matching `chsh_settings`, `qkd_settings`, `rng_settings`) | Names are provisional ("for now"); a missing key keeps today's behaviour | `"entangled"` / `"low_entanglement"` |
| 9 | No mode coordination between Nodes at game time: each Node plays in its own configured Operation mode. Whobot's digest reports each Node's mode and flags a pair whose modes differ | Both Nodes see the same source, so a mismatch is an operator mistake; keep it simple | The leader's mode winning for the game; refusing to play on a mismatch |
| 10 | Whobot's digest plays SSM, as its own phase of this plan | Today the digest always skips SSM; the new mode is SSM-only, so it needs an unattended check | Leaving SSM unchecked |
| 11 | A Node endpoint plays one unattended SSM game, as `POST /chsh` does for CHSH: Whobot sends both players' Answers to the leader, which puts the follower in the follower role without asking anyone, runs the existing SSM loop, resets coordination on both Nodes and returns the result. It always runs, whatever either Node is doing | Reuses the game code; doesn't touch the kiosk's websocket; the digest runs early morning when the library is closed | Whobot driving the GUI's websocket dance (only one socket gets the prompt; either disconnect stops follow requests); running only when the follower is idle (a bespoke busy check) |
| 12 | The digest plays SSM once per Node, with that Node as leader and its configured follower, as it does for CHSH; a pair is therefore checked in both directions | Same shape as CHSH; covers both leader/follower assignments | — |
| 13 | The digest's SSM check depends on the Node's Operation mode. **Entanglement:** `ssm_questions` (whobot.toml, default 8) questions, all with matching Answers, half both-left (H/V) and half both-right (D/A); the unattended endpoint returns the leader's inferred bits and the follower's actual bits; Whobot reports the error rate per basis; OK when both are ≤ `ssm_max_error_rate` (default 0.15), WARN otherwise. **Correlation:** the four Answer combinations repeated to N questions; OK when every question gets its expected Coincidence level, WARN otherwise, and each measured rate is shown next to its Reference rate. **Both:** FAIL when the game does not complete; the report states the mode. The endpoint takes any number of questions, independent of `bitstring_length` and the GUI's question order | Checks the actual key bits, so fading entanglement shows up as D/A error near 50% while H/V stays fine; returning bits only from the unattended endpoint leaves the GUI game unchanged (decision 4) | Four fixed mixed Answers in both modes (only 2 compared bits in entanglement mode); checking only that the game completes |
| 14 | Whobot gets a "Run SSM" Action that plays one unattended SSM game on demand. Every digest default (questions, Answers, error threshold, integration time) is a form field the operator can change, prefilled from config and the Node's Operation mode | Same shape as Run CHSH and its prefill | — |
| 15 | Run SSM picks only the leader; the follower is that Node's configured `follower_node_address`, as in Run CHSH and Angle Sweep. To check the other direction, pick the other Node | Whobot's Action framework picks one Node (`Scope.ONE`); the Node already resolves its follower this way | Picking both Nodes (needs a follower-address override and a two-Node form) |
| 16 | Two Node endpoint pairs, `GET`/`PUT /node/operation_mode` and `GET`/`PUT /node/calibration` (Calibrated angles and Reference rates), each writing `config.toml` through `update_config` and applying live, as `PUT /games/availability` does. Two `Scope.ONE` Whobot Actions, **Change Operation mode** (enum dropdown) and **Change calibration** (four angle floats, three rate floats), each prefilled from the Node | Mode switching is a quick daily call; calibration is careful numeric tuning, kept apart so a mode switch can't change numbers by accident | One combined Action |
| 17 | The GUI changes are the last phase of this plan, built in their own pqn-gui Orca worktree (`/home/marcosf2/orca/workspaces/pqn-gui/operation_mode`, branch `marcosfrenkel/operation_mode`, a child of this worktree) with the same agent workflow; not stacked, since it is another repo. This plan is the single tracker. The pqn-node phases fix the contract: `GET /node/operation_mode`, and an unchanged `QKDResult` in both modes. Marcos writes the user-facing text, recorded in this plan | One tracker across both repos; the GUI needs no new player steps | A separate plan in pqn-gui; doing the GUI by hand |
| 18 | GUI text in correlation mode (see [User-facing text](#user-facing-text)); the SSM start screen does **not** say the game takes longer, since nothing is calibrated per game (decision 2). The "Learn more" link points to https://publicquantumnetwork.org for now; to be replaced by the explanation page later | Marcos wrote it | — |
| 19 | Phases as listed in [Phases](#phases): 0 design and workflow, 1 settings and Whobot controls, 2 correlation-mode SSM, 3 unattended SSM and the digest, 4 pqn-gui, 5 deploy. Whobot's settings Actions ship in Phase 1 so real angles can be set before Phase 2 uses them | Vertical slices, each usable on its own | Folding all Whobot work into Phase 3 |
| 20 | Workflow as in [Workflow](#workflow): the Overall orchestrator runs in this worktree and starts each Phase orchestrator (including Phase 4's) in that phase's worktree; each pqn-node phase starts with a fast-forward onto the previous branch; the plan travels with the stack; Phase 4's plan commits land on `unattended_ssm`; restacking is Marcos's | Each PR reviewable in its own worktree, with one orchestrator overseeing the project | One canonical plan copy in this worktree |
| 21 | Testing as in [Testing rule](#testing-rule). Phase 4 (pqn-gui, which has no tests) runs `lint`, `format:check` and `build` as its checks, without the Monkey or the test reviewers, plus a by-hand visual check of both modes by Marcos | The Monkey needs tests to mean anything; the GUI change is small conditional text | Adding Vitest + React Testing Library in Phase 4 |
| 22 | The Overall orchestrator runs unattended: it starts at the first open phase, runs Phases 1→2→3→4 back to back, and never waits for Marcos between phases. It still stops for hard stops. Its final report lists the PRs to open (base branch for each), the agent decisions to review (`⚠` first) and the manual items (Phase 4's visual check, Phase 5) | Each phase has its own worktree, so Marcos reviews the code there afterwards; waiting between phases served the old single-worktree flow | Asking Marcos which phase to run and waiting for his go after each phase |
| 23 | Marcos is the only author of every commit and PR. No `Co-Authored-By` trailers, "Generated with" footers, and no line crediting Claude or any AI, in commit messages or committed files. (Workflow files may still name the `claude` CLI as a runner.) Written into every role file and spec | Marcos's standing rule | — |

### Agent decisions (pending review)

| # | Date | Group | Asked by | Question | Decision | Alternatives | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A14 | 2026-10-27 | 4.1 | Phase orchestrator | What does the GUI do when `GET /node/operation_mode` fails (Node unreachable, an error status, or a value other than `entanglement` / `correlation`)? | Treat it as `entanglement`: no note is shown and the Games play as today. Only a Node that answers `correlation` shows the correlation text (decision 8: a missing key keeps today's behaviour). Same shape as `fetchGamesAvailability`, which falls back to today's behaviour when the Node does not answer | Show the note when unsure; block the Games until the mode is known | Pending |
| A13 | 2026-10-27 | 3.3 | Phase orchestrator | Decision 9's digest "flags a pair whose modes differ". How does Whobot learn the follower's Operation mode, and what if it cannot? | The digest reads the Node's `follower_node_address` (`GET /node/config`) and asks that address for `GET /node/operation_mode` directly, the address the Node itself uses for its follower (decision 15). Same mode: no flag. Different: WARN naming both modes. No follower configured: no flag (and SSM cannot play). The follower's mode cannot be read: the follower's mode shows as unknown and WARNs, so a mismatch is never hidden. The follower need not be in the registry | Look the follower up in the registry by address (fails for a follower Whobot does not list); leave an unreadable follower unflagged |  Pending |
| A12 | 2026-10-27 | 3.2 | Phase orchestrator (plan-checker-glm) | Decision 13 shows "each measured rate next to its Reference rate" in correlation mode. The Reference rate of the level the two Answers expect, or of the level measured? | The expected level's: both Answers left → VV, both right → DD, mixed → VD (decision 1). A row then reads as "what we got vs what we should have got", and a misclassified question stands out. The status still follows only the levels (OK when measured = expected) | The measured level's Reference rate (always the nearest, so it says nothing new); all three rates on every row | Pending |
| A11 | 2026-10-27 | 3.2 | Coder | Entanglement-mode SSM report: what if an operator's Answers leave a basis with no compared question (no question where both Answers pick it)? | That basis shows "not compared" and the status is WARN, never OK: decision 13 says OK only "when both are ≤ `ssm_max_error_rate`", and an unchecked basis is not ≤ anything. The threshold is inclusive (≤). The digest's default Answers (all matching, half each basis) always compare both | OK when every compared basis is under the threshold; FAIL |  Pending |
| A10 | 2026-10-27 | 3.2 | Coder | Decision 14 makes the Answers form fields, but Whobot's forms render only bool, float, int and Enum. How are Answers typed? | Add `str` to `WIDGET_TYPES` and one plain-text branch to the Slack form renderer, the extension the framework's own docstring describes ("Adding `str` is one entry here and one branch"). **Run SSM** has two text fields, the leader's and the follower's Answers, as strings of `a` (left) and `b` (right), e.g. `aaaabbbb`, prefilled from the Node's Operation mode for `questions` questions: entanglement all matching, the first half both `a` and the rest both `b` (decision 13); correlation the four combinations in turn, leader `aabb…`, follower `abab…`. Both fields empty → those defaults. Otherwise only `a`/`b`, the two the same length (decision 14 "refuses unequal-length Answers") and that length equal to `questions`, or the Action refuses before calling the Node | Only a question count, with Answers derived and not editable (against decision 14); one `str` field for both players |  Pending |
| A9 | 2026-10-27 | 3.2 | Coder | The plan gives no default integration time for **Run SSM**, and Whobot cannot read the Node's. What does the form prefill? | `integration_time_s` prefilled with 0, meaning "the Node's own configured integration time": 0 sends no `integration_time_s`, so the Node measures as its GUI game does; any value > 0 is sent as is. The digest (3.3) sends none either. No new Node read and no new Whobot key (Surgical and minimal) | A new `whobot.toml` key; reading the Node's measurement config through a new endpoint; a fixed default such as Angle Sweep's 1.0 s |  Pending |
| A8 | 2026-10-27 | 3.1 | Marcos (glm-5.3 errors) | Which models do the coder and the three glm reviewers run on in Phase 3? | **Marcos's request:** every agent the roster puts on `lumen/glm-5.3` (`coder`, `reviewer-glm`, `test-reviewer-glm`, `plan-checker-glm`) runs on `lumen/deepseek-v4.1-flash` instead, same agent ids, launched with `opencode mini --agent <id> --model lumen/deepseek-v4.1-flash`. The 3.1 coder had started on glm-5.3; it was stopped (Orca `worker-stop`, terminal closed) and the task restarted on deepseek. Closing the terminal did not end the glm session in opencode's background service: it went on editing `qkd.py` and wrote the first test file before it stopped and escalated, so the deepseek coder took over all of that work and commits it as one change. `ROSTER.md` is not edited | Keep glm-5.3 and switch only on errors; the phase 2 split (coder on qwen3.8-27b) | Pending |
| A7 | 2026-10-27 | 2.1 | Phase orchestrator (round limit, Monkey recheck 5) | After the fifth fix round all six reviewers approve, but a new random batch leaves 10 real survivors: no game of 5+ questions (default `bitstring_length` 6) in either mode, a one-question follower game, Reference-rate sets outside the tested ratios (VV > 2.5·VD, VD < 2·DD, rates above 10⁴ cps), games longer than 8 questions, and a 0.15 cps boundary shift (M118, M120, M121, M123, M124, M127, M128, M133, M136, M137). Stop, close with them open, or allow one more round? | **Marcos approved** one sixth fix round, tests only, for exactly these 10 survivors. The recheck re-applies only those 10 patches, with no new random batch. 2.1 closes if all 10 are killed. The round limit (Workflow "Round limit: 5") stays 5 for every other group | Hard stop with 2.1 left `[~]`; close now with the 10 survivors logged as open | Pending |
| A6 | 2026-10-27 | 1.3 | Phase orchestrator (reviewer-glm finding) | When **Change calibration**'s prefill cannot read the Node, Whobot's framework opens the form on the signature defaults (today's angles, A2's rates). Submitting that form would overwrite a calibrated Node with defaults. Refuse to open the form instead? | Keep the framework's existing behaviour (`Whobot._initial_params`: "a form opening on defaults beats no form", the same for the availability Action). The operator sees every value before submitting, and a Node that cannot be read usually cannot be written either. Changing it is a framework-wide change outside group 1.3 | Refuse to open a prefilled form when its prefill fails (for this Action only, or for all); a warning field in the form | Pending |
| A5 | 2026-10-27 | 1.2 | Phase orchestrator (Monkey recheck 4) | Monkey mutants that add range checks to `PUT /node/calibration` (no negative VV, no inf/NaN, \|H\| ≤ 360, DD < 10⁹) survive, because nothing pins that such values are accepted. Range-check the Calibrated angles and Reference rates, or pin their acceptance with tests? | Neither: any float is accepted, as decision 16 states ("four angle floats, three rate floats"), and those mutants are dropped. Pinning that nonsense values are accepted would encode no requirement; adding range checks is new behaviour the plan does not ask for. Also dropped: an int D stored as int, and NaN (not valid JSON) stored as 0. Group 1.2 closed after 4 of 5 rounds | Range validation on the request models (a new contract for Marcos to choose); a fifth round of tests pinning acceptance | Pending |
| A4 | 2026-10-27 | 1.2 | Phase orchestrator (test-reviewer-glm, test-reviewer-qwen, Monkey) | `PUT /node/calibration` reuses the settings models, whose fields have defaults, so a partial body is accepted and silently resets the omitted Calibrated angles or Reference rates to their defaults. Accept partial bodies (merging with the current values) or refuse them? | Refuse: the body must carry all four angles and all three rates; anything missing or non-numeric is a 422 that writes nothing. Decision 16 has **Change calibration** send "four angle floats, three rate floats", so a full body is the contract; refusing is the smaller change and cannot reset a value by accident | Partial update merged with the current values (a PATCH-like contract no caller needs); keeping today's silent reset to defaults | Pending |
| A3 | 2026-10-27 | 1.1 | Phase orchestrator (test-reviewer-glm finding) | `/chsh/request-angle` receives only a float. Should a custom Angle Sweep pin that equals a named polarization's angle (0, 45, 90, 315) move raw, as decision 6 says for custom angles? | No: a pin at exactly a named angle is that named polarization and moves to its Calibrated angle. A test pins this behaviour. Smallest change: the API and Whobot's payload stay as they are (decision 6; Surgical and minimal) | Adding a "named" flag to `/chsh/request-angle` and to Whobot's sweep payload, so a custom 45° stays raw | Pending |
| A2 | 2026-10-27 | 1.1 | Coder | The plan gives no default Reference rates for `[correlation_settings]`. What should ship? | `vv = 3000`, `vd = 1500`, `dd = 750` coincidences per second: decision 1's expected levels (VV 1, VD ½, DD ¼) scaled to the whiteboard's ≈3K VV (decision 1, rejected alternative). Nothing reads them until Phase 2, and Phase 5 sets real values with **Change calibration** | All 0.0 as an "operator must set" placeholder (every question would then classify to the same level) | Pending |
| A1 | 2026-10-27 | 1.1 | Phase orchestrator | `ROSTER.md`'s opencode launch commands (`opencode --agent <id>`) fail: opencode on this machine is now v2.0.32, whose TUI rejects `--agent`. How to start opencode workers? | Launch each opencode worker with `PYTHONDONTWRITEBYTECODE=1 opencode mini --agent <id> --model <roster model>`. Checked that the status bar shows `<id> · <model> via Lumen` and that Orca's `worker-start` accepts it. Same agents, models and `opencode.json` permissions as the roster (Workflow "Roles and loop"; `ROSTER.md` model-id note). `ROSTER.md` is not edited; the overall orchestrator was told | Hard stop until Marcos fixes the roster; per-terminal `OPENCODE_CONFIG_CONTENT` `default_agent` (ignored, the TUI uses a background service); `mini` without `--model` (silently runs "Flint 40B Free") | Pending |

## Phases

Each pqn-node phase is one PR, stacked on the previous one, in its own Orca worktree. **Every action ships with its tests in the same PR.** Defaults reproduce today's behaviour until an operator changes a setting.

### Phase 0: Design and workflow (PR 0, by hand)

- [x] Commit `CONTEXT.md` and this plan
- [x] Adapt `.agents/` for pqn-node: point roles and skill at this plan; match its CI (`uv lock --locked`, `ruff check`, `ruff format --check`, `mypy`, `pytest`, `uv build`)
- [x] Adapt `.agents/` for pqn-gui: point at this plan (in the pqn-node worktree); checks are `npm run lint`, `npm run format:check` and `npm run build`; no Monkey or test reviewers (decision 21)
- [x] Add `orchestration/` (and Orca's `.orca/`) to `.gitignore` in pqn-node and pqn-gui
- [x] Overall orchestrator runs unattended across worktrees (decisions 20, 22): runs Phases 1→2→3→4 back to back, fast-forwarding each pqn-node phase branch first; no stop between phases; ends with one report listing the PRs to open, the agent decisions to review and the manual items
- [x] Authorship rule in every role and spec: Marcos is the only author; no `Co-Authored-By` trailers, no "Generated with" footers, no line crediting Claude or any AI in commits or committed files (decision 23)
- [x] Commit Phase 0 in pqn-node and the workflow files in pqn-gui
- [ ] **Manual:** install Node.js 20 on this machine (pqn-gui's CI version; not installed today), then run `npm ci` in `pqn-gui/operation_mode`. Blocks Phase 4 only

### Phase 1: Settings and Whobot controls (PR 1)

No Game behaves differently in this phase.

Node settings:
- [x] `OperationMode` `StrEnum` (`entanglement`, `correlation`) in `constants.py`; top-level `operation_mode` setting, default `entanglement` (decision 8)
- [x] `[polarization_angles]` with H, V, D, A waveplate angles, defaults equal to `QKDAngleValuesHWP` (decision 5)
- [x] Every move to a named polarization uses the Calibrated angles: SSM leader and follower moves, and the Angle Sweep's follower pin when it is H/V/D/A. CHSH, the sweep's leader steps and a custom follower angle stay raw (decision 6)
- [x] `[correlation_settings]` with `reference_rates` (VV, VD, DD, coincidences per second) (decision 7)
- [x] Update `configs/config_example.toml`

Node API:
- [x] `GET`/`PUT /node/operation_mode` and `GET`/`PUT /node/calibration`, writing `config.toml` through `update_config` and applying live (decision 16)

Whobot:
- [x] **Change Operation mode** Action (`Scope.ONE`, enum dropdown, prefilled) (decision 16)
- [x] **Change calibration** Action (`Scope.ONE`, four angle floats and three rate floats, prefilled) (decision 16)

Tests:
- [x] Config round-trip: each new key written by `update_config` survives with comments and is applied live
- [x] A Node with no new keys behaves exactly as today (angles, mode)
- [x] Named moves use the configured angle; continuous moves don't
- [x] Both Actions: prefill reads the Node; submit sends the right payload; Node errors become a FAIL report

### Phase 2: Correlation-mode SSM (PR 2)

- [x] In `correlation` mode, the leader and the follower move to V or D (Calibrated angles) by their own Answer, with no random bit (decision 1)
- [x] The leader converts each question's count to a rate using the integration time it measured with, and classifies it by the nearest Reference rate: high or low → agree, medium → disagree (decisions 1, 7)
- [x] `n_matching_bits` in correlation mode is the number of questions classified as agree; `QKDResult` keeps its shape (decision 17)
- [x] In `correlation` mode the leader never fetches the follower's Answers (`/qkd/request_basis_list`): agreement comes only from the Coincidence levels (decisions 1, 3). Test: the follower mock fails the test if that endpoint is called.
- [x] Entanglement mode unchanged (decision 4)
- [x] Tests: each Answer combination moves the right angles on each side; classification at, between and beyond each reference; a game end to end with a fake time tagger in both modes; the follower's Answers are never fetched in correlation mode

### Phase 3: Unattended SSM and the digest (PR 3)

Node:
- [x] `POST /qkd/unattended` on the leader: takes both players' Answers (any equal length), puts the follower in the follower role without asking anyone, always runs, plays the existing loop, resets coordination on both Nodes, returns the result. Entanglement: the leader's inferred bits and the follower's actual bits per question. Correlation: each question's rate and Coincidence level (decisions 11, 13)

Whobot:
- [x] **Run SSM** Action (`Scope.ONE`, leader = chosen Node, follower = its configured follower): questions, leader and follower Answers, max error rate, integration time, all prefilled from config and the Node's mode and editable; refuses unequal-length Answers (decisions 14, 15)
- [x] SSM report section shared by the Action and the digest: per-basis error rate (entanglement) or per-question rate vs Reference rate and expected vs measured level (correlation); OK / WARN / FAIL as in decision 13
- [x] `whobot.toml`: `ssm_questions` (default 8), `ssm_max_error_rate` (default 0.15)
- [x] Digest: play SSM on each Node where it is available, with that Node as leader; report each Node's Operation mode; flag a Node whose follower is in a different mode; grow the per-Node budget to cover SSM (decisions 9, 12)

Tests:
- [x] Unattended endpoint: both modes; follower already in a game is taken over; coordination reset afterwards, also on failure
- [x] Report statuses: OK, WARN (error rate above threshold; misclassified question), FAIL (game did not complete)
- [x] Digest includes an SSM section per Node and the mode mismatch flag

### Phase 4: pqn-gui (own worktree, not stacked)

Worktree `/home/marcosf2/orca/workspaces/pqn-gui/operation_mode`, branch `marcosfrenkel/operation_mode`. Starts after Phase 1 is deployed somewhere it can call.

- [x] Read `GET /node/operation_mode`
- [x] Home screen and CHSH start-screen notes in correlation mode ([User-facing text](#user-facing-text))
- [ ] SSM start-screen note with the Learn more link, and the result-page sentence, in correlation mode
- [ ] **Manual:** Marcos checks both modes in the GUI before the PR

### Phase 5: Deploy and lab check (by hand)

- [ ] Deploy pqn-node, Whobot and pqn-gui to both Nodes
- [ ] Set real Calibrated angles and Reference rates with **Change calibration**
- [ ] **Run SSM** in both directions and both modes on hardware

## Workflow

Phases 1–4 are built by agents, using the role files and skill in [`.agents/`](../.agents/) (copied from pqn-hardware's JSON wire protocol work). Phases 0 and 5 are done by hand. Every agent runs in its own visible Orca terminal.

### What differs from pqn-hardware (decision 20)

- **One worktree per phase.** The Overall orchestrator runs in this worktree (`pqn-node/low_entanglement_mode`) for the whole project. For each phase it launches the Phase orchestrator in that phase's worktree (`orca terminal create --worktree path:<phase worktree>`, then `worker-start --worktree path:<phase worktree>`). The Phase orchestrator launches its Coder, Reviewers and Monkey in its own worktree, as before.
- **Start of a pqn-node phase.** The Overall orchestrator fast-forwards the phase branch onto the previous phase's branch: `git -C <phase worktree> merge --ff-only <previous branch>`. If that fails (the branch has its own commits), it is a hard stop for Marcos.
- **The plan travels with the stack.** It is committed on every branch. The live copy is the one in the running phase's worktree: the Phase orchestrator ticks boxes and writes Work log rows there, and the Overall orchestrator reads it and commits the Status row there. Earlier worktrees keep the version their PR shipped.
- **Phase 4 (pqn-gui)** is started by the Overall orchestrator too, in `pqn-gui/operation_mode`, with no fast-forward (another repo, based on pqn-gui's `master`). Its Phase orchestrator reads and commits the plan in `pqn-node/unattended_ssm` (plan-only commits), and runs pqn-gui's own CI checks instead of the uv ones.
- **Restacking is Marcos's.** Agents never rebase. If an earlier PR changes after review, Marcos moves the later branches.

### Unchanged from pqn-hardware

- **Surgical and minimal.** Every change is the smallest diff that meets the task, reusing existing code. Reviewers and plan checkers flag violations as `must-fix`.
- **Roles and loop.** One Coder, six Reviewers (general, tests, plan checker, each on two models), one Monkey per checkbox group; fix rounds until clean; round limit 5. See [`ROSTER.md`](../.agents/roles/ROSTER.md) and the [`orchestrate-plan`](../.agents/skills/orchestrate-plan/SKILL.md) skill.
- **Decide, don't wait.** The Phase orchestrator answers questions the plan doesn't, and logs each in [Agent decisions (pending review)](#agent-decisions-pending-review), marked `⚠` if it goes against a recorded decision. Hard stops only for broken rules, the round limit, or needing something outside the repo.
- **Phase end (changed, decision 22).** The Overall orchestrator marks the phase done in [Status](#status) and goes straight on to the next phase. Marcos reviews each PR in its own worktree afterwards; nobody waits for him between phases. Agents never push or open PRs.
- **Authorship (decision 23).** Marcos is the only author of every commit. No `Co-Authored-By` trailers, no "Generated with" footers, no line crediting Claude or any AI in commit messages or committed files.
- **Working files.** `orchestration/` is git-ignored, per worktree.

### Checkbox groups

| Group | Checkboxes | Already done (by earlier groups) |
| --- | --- | --- |
| 1.1 | Phase 1 "Node settings", plus the config and no-change tests | — |
| 1.2 | Phase 1 "Node API", plus their config round-trip tests | — |
| 1.3 | Phase 1 "Whobot", plus the Action tests | — |
| 2.1 | All of Phase 2 | — |
| 3.1 | Phase 3 "Node", plus the unattended endpoint tests | — |
| 3.2 | Phase 3 "Whobot" except the digest item, plus the report status tests | — |
| 3.3 | Phase 3 digest item, plus the digest test | — |
| 4.1 | Phase 4 first two items | — |
| 4.2 | Phase 4 third item | — |

## Testing rule

Every change ships with its own tests in the same PR (decision 21).

pqn-node, three kinds:

1. **Config.** Every new key is written by `update_config` with comments kept, applied live, and a config without the key behaves as today (pattern: `test_config_updates.py`).
2. **Node games with a fake photon source.** Waveplates are `FakeWaveplate` through `hardware.Client` (pattern: `test_node_api_additions.py`). `measure_coincidences` is replaced by a **source model** that returns counts from the two waveplate angles: a product-VV source for correlation mode, an entangled state for entanglement mode. Node state is module-global, so the follower is an `httpx.MockTransport` answering the follower endpoints; the leader is real.
3. **Whobot.** `NodeClient` over an `httpx.MockTransport` (pattern: `test_whobot_*`): prefills, payloads, and OK / WARN / FAIL for each case.

pqn-gui: no test suite. Checks are `npm run lint`, `npm run format:check` and `npm run build`; no Monkey or test reviewers; Marcos checks both modes by eye before the PR.

## Out of scope and deferred

- **A dedicated calibration measurement** for the correlation-mode SSM values, or Whobot suggesting them from an Angle Sweep. Later; for now they are set by hand (decision 2).
- **A general "Node is busy" mechanism** shared by every Game and by Whobot. Not a bespoke one for SSM (decision 11).
- **Fixing entangled-mode SSM** so it compares key bits (decision 4).

## Risks

- **Medium vs low is only a factor of 2** in correlation mode (½ vs ¼ of high). Drift in source brightness or fiber polarization misclassifies questions. Mitigation: Reference rates are easy to update through Whobot, and the digest's correlation check shows each measured rate next to its reference.
- **Modes differ between a pair of Nodes.** Each Node plays in its own mode (decision 9), so a mismatch gives a meaningless game. Mitigation: the digest flags it every morning.
- **The unattended game always runs** (decision 11), so a manual Run SSM during opening hours takes over a visitor's game. Mitigation: the digest runs before opening; a general "Node is busy" mechanism is deferred.
- **Calibrated angles change the entanglement-mode game too** (decisions 4, 6). A wrong value breaks today's SSM. Mitigation: defaults equal today's values; Phase 5 checks both modes on hardware.
- **Stacked branches drift** if an earlier PR changes after review. Mitigation: restacking is Marcos's, by hand; a phase's fast-forward refuses to run on a branch with its own commits.

## Work log

Newest first. One row per session or meaningful event.

| Date | Who | What happened |
| --- | --- | --- |
| 2026-10-27 | Phase orchestrator | Group 4.1 done (pqn-gui, `marcosfrenkel/operation_mode`): `fetchOperationMode` in `src/calls.tsx` reads `GET /node/operation_mode`. When the request fails or answers anything other than `entanglement` / `correlation`, it gives `entanglement` (A14). In correlation mode the home screen and the CHSH start screen show the plan's notes word for word, inside Whobit's speech bubble. The home screen refetches the mode when the attract loop is dismissed, as it does for availability. Commit `df504fd`. 0 fix rounds of 5; all four Reviewers approved first time. One nit (the `refreshGamesAvailability` name) was not sent. No Monkey or test reviewers (decision 21). Agent decision A14. Whether the extra line fits Whobit's bubble is left to Marcos's visual check. Checks: lint clean, build OK (15/15 pages), and format:check flags only the 7 files already failing at the base (`.agents/*`, `status-bridge/server.mjs`, `tsconfig.json`) |
| 2026-10-27 | Phase orchestrator | Group 3.3 done (all of Phase 3 now done): the Daily Digest plays SSM on every Node that offers it, with that Node as leader and its configured follower. It uses `ssm_questions`, the mode's default Answers, `ssm_max_error_rate` and no integration time (A9), through the shared section from 3.2; a Node that does not offer SSM gets SKIPPED. Every reachable Node gets an "Operation mode" section stating its mode, whatever it offers (decision 9). Its follower row is A13: the follower's mode is read at the Node's `follower_node_address`; a different mode WARNs naming both, an unreadable one shows "unknown" and WARNs, and no follower configured gets no warning. `one_node_budget` grew to 6 Node reads + 3 Games. The old "interactive coordination dance" skip is gone. Tests in `test_whobot_flow.py`. Commits `75b6edf`, `abbce53`, `fe113e1`, `b5ba8bc`. 3 fix rounds of 5 (1 review, 2 monkey). The monkey ran 49 + 25 + 21 mutants: all 6 survivors were real and killed, none were left open. The monkey hunt ran in parallel with re-review 1, since the source no longer changed. Agent decision A13. 562 tests pass, all six checks green |
| 2026-10-27 | Phase orchestrator | Group 3.2 done: Whobot **Run SSM** Action (`Scope.ONE`, `ssm_budget` = game + two Node reads). Its form has questions, the two players' Answers as `a`/`b` text fields (A10, a new `str` form widget), max error rate and integration time (0 = the Node's own, A9). Prefills come from `whobot.toml` and the Node's Operation mode. Unequal-length or wrong Answers are refused before the Node is called. The shared SSM report section (`_play_ssm`, which the digest will call) states the mode in every section, also on FAIL. In entanglement mode it gives each basis's error rate over the questions whose Answers match, OK when both are ≤ the max (a basis no question compared WARNs, A11). In correlation mode it gives each question's rate beside the expected level's Reference rate (A12), and the status follows the levels. `NodeClient.run_ssm` parses the result by its `mode`. `whobot.toml`: `ssm_questions` (8) and `ssm_max_error_rate` (0.15). Tests in `test_whobot_flow.py`, `test_whobot_slack.py`, `test_whobot_config.py` and `test_whobot_registry.py`. Commits `32bba25`, `1bd5352`, `b90f1da`. 2 fix rounds of 5 (1 review, 1 monkey). The monkey ran 99 + 28 mutants: 8 real survivors killed, 5 equivalent or duplicate ones dropped. A reviewer's leftover repo copies under `orchestration/` broke one full test run and were removed. Agent decisions A9–A12. 548 tests pass, all six checks green |
| 2026-10-27 | Phase orchestrator | Group 3.1 done: `POST /qkd/unattended` on the leader in `api/routes/qkd.py`. It takes both players' Answers ('a'/'b', equal length, non-empty, else 400), an optional `integration_time_s` (> 0) and the time tagger address. It puts the configured follower in the follower role through a new `POST /qkd/unattended/follower` (no one is asked, a game in progress is taken over) and plays the existing `_qkd` loop. It then resets coordination on both Nodes, also when the game fails (the follower's reset is best-effort). The result has a `mode` field: entanglement gives `leader_bits` and `follower_bits`, one per question (the follower's from a new `GET /qkd/unattended/follower_bits`); correlation gives each question's rate and Coincidence level. The GUI endpoints return what they did before. Tests in `test_unattended_ssm.py`. Commits `7dbd2ff`, `dceee4e`, `af3b445`. 2 fix rounds of 5 (1 review, 1 monkey). The monkey ran 129 + 29 mutants: 9 real survivors killed, 12 equivalent ones dropped (log lines, unobservable defaults). At Marcos's request the coder and the three glm reviewers ran on deepseek-v4.1-flash (A8). The first glm coder kept editing in opencode's background service after its terminal was closed; the deepseek coder took over its draft. Agent decision A8. 495 tests pass, all six checks green |
| 2026-10-27 | Phase orchestrator | Group 2.1 done (all of Phase 2): correlation-mode SSM in `api/routes/qkd.py`. Leader and follower move to the Calibrated V or D by their own Answer, with no random bit. The leader turns each count into a rate using the integration time it measured with and classifies it by the nearest Reference rate (`CoincidenceLevel` in `constants.py`): high or low → agree, medium → disagree. `n_matching_bits` counts the agreements, and the follower's Answers are never fetched. Entanglement mode unchanged. Tests in `test_ssm_operation_modes.py`. Commits `6828f03`, `e990245`, `b417083`, `e9904b5`, `37c8335`, `e4a4034`, `ea368c3`, `3778cf4`. 6 fix rounds: 5, plus one extra round Marcos approved (A7). Two of the rounds carried review findings, all six carried monkey survivors. The monkey ran 6 hunts plus the A7 recheck: every real survivor killed, equivalent mutants dropped with reasons. Mid-run, at Marcos's request, the coder moved from glm-5.3 to qwen3.8-27b and the three glm reviewers to deepseek-v4.1-flash, after Lumen timeouts. A crashed coder session resumed as the default Build agent and committed the plan's A7 edit itself (`3778cf4`); the commit was kept, since history is never rewritten. Agent decision A7. 474 tests pass, all six checks green |
| 2026-10-27 | Phase orchestrator | Group 1.3 done: Whobot **Change Operation mode** (`Scope.ONE`, `OperationMode` dropdown) and **Change calibration** (`Scope.ONE`, four angle and three rate floats) Actions, prefilled from the Node, with `NodeClient` methods for `/node/operation_mode` and `/node/calibration`; tests in `test_whobot_flow.py` and `test_whobot_registry.py` (prefill, payload, FAIL on Node errors and wrong-shape answers, reports show the Node's answer, timeouts). Commits `1ffb799`, `5e48a6c`, `b48295d`. 2 fix rounds of 5 (1 review + monkey, 1 monkey); monkey 3 hunts, all real survivors killed. Agent decision A6 (prefill fallback kept). Phase 1's checkbox groups are all done. 426 tests pass, all six checks green |
| 2026-10-27 | Phase orchestrator | Group 1.2 done: `GET`/`PUT /node/operation_mode` (bare mode string) and `GET`/`PUT /node/calibration` (Calibrated angles + Reference rates; every value required, A4), writing `config.toml` through `update_config` and applying live; endpoint round-trip and refusal tests in `test_node_api_additions.py`. Commits `5aeacaa`, `b17dbb9`, `d98bcd5`, `648784e`, `2619ab9`. 4 fix rounds of 5 (1 review + monkey, 3 monkey); monkey 5 hunts, all real survivors killed; range-check mutants dropped as A5. Agent decisions A4, A5. "Config round-trip" box now done (1.1 + 1.2). Marcos's plan change for Phase 2 committed separately (`088ffd5`). 393 tests pass, all six checks green |
| 2026-10-27 | Phase orchestrator | Group 1.1 done: `OperationMode` and top-level `operation_mode`, `[polarization_angles]` (Calibrated angles, defaults = `QKDAngleValuesHWP`), `[correlation_settings.reference_rates]`, SSM moves and the Angle Sweep's named follower pin on the Calibrated angles, `config_example.toml`, tests in `test_node_settings.py`. Commits `2903d1b`, `b4cf052`, `12ec01a`. 2 fix rounds (1 review, 1 monkey) of 5; monkey 80 + 20 mutants, survivors M3/M79/M10 killed, the log-line mutant and an equivalent one dropped. Agent decisions A1 (opencode v2 launch command), A2 (default Reference rates), A3 (a custom pin at a named angle is the named pin). The "Config round-trip" box stays `[~]` until 1.2 adds the endpoints. 356 tests pass, all six checks green |
| 2026-10-27 | Marcos | Phase 0 done. `.agents/` adapted for pqn-node (this plan, six CI checks incl. `uv build`, pqn-node examples, authorship rule, Overall orchestrator rewritten for unattended runs across worktrees) and for pqn-gui (npm checks, no Monkey or test reviewers, plan read and committed in `pqn-node/unattended_ssm`; opencode allows the npm checks). `orchestration/` and `.orca/` git-ignored in both repos. Node.js is not installed on this machine, so pqn-gui's `npm ci` is left as a manual item; the Overall orchestrator checks for it before Phase 4. Phase 1 is next: start the Overall orchestrator in this worktree. |
| 2026-10-27 | Marcos | Decisions 22 (unattended Overall orchestrator, no review stop between phases) and 23 (authorship) recorded; Phase 0 list updated. |
| 2026-10-27 | Marcos | Decision 21: testing rule. Summary, Testing rule and Risks written. Design session finished; no open questions. Next: Phase 0 workflow tasks, then commit. |
| 2026-10-27 | Marcos | Decision 20: workflow across worktrees. Workflow section written. |
| 2026-10-27 | Marcos | Decision 19: phases written. Created Orca worktrees for Phases 1–3, each branched from the previous phase's branch (all at `2536160`, empty). |
| 2026-10-27 | Marcos | Decision 18 amended: no "takes longer" sentence. |
| 2026-10-27 | Marcos | Decision 18 and the user-facing text recorded. |
| 2026-10-27 | Marcos | Decision 17 recorded. Created the pqn-gui worktree `operation_mode` (base `ae7aed9`). Copied `.agents/` and `opencode.json` from pqn-hardware's `moving_to_json` worktree into this worktree and the pqn-gui one, unadapted. |
| 2026-10-27 | Marcos | Decision 16 recorded. |
| 2026-10-27 | Marcos | Decision 15 recorded. |
| 2026-10-27 | Marcos | Decisions 13 and 14 recorded. |
| 2026-10-27 | Marcos | Decisions 11 and 12 recorded. |
| 2026-10-27 | Marcos | Decisions 9 and 10 recorded. |
| 2026-10-27 | Marcos | Decision 8 recorded (mode values `entanglement` / `correlation`, provisional). |
| 2026-10-27 | Marcos | Decision 7 recorded. |
| 2026-10-27 | Marcos | Decisions 5 and 6 recorded; decision 4 amended. |
| 2026-10-27 | Marcos | Decisions 2 (amended: values in config, no per-game calibration), 3 and 4 recorded. |
| 2026-10-26 | Marcos | Design session (grill-with-docs) started on branch `marcosfrenkel/low_entanglement_mode` (base `2536160`). Plan skeleton created. |
