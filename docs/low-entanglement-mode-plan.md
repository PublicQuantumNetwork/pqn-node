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
| 1. Settings and Whobot controls | Not started | `marcosfrenkel/operation_mode_settings` · `pqn-node/operation_mode_settings` |
| 2. Correlation-mode SSM | Not started | `marcosfrenkel/correlation_ssm` · `pqn-node/correlation_ssm` |
| 3. Unattended SSM and the digest | Not started | `marcosfrenkel/unattended_ssm` · `pqn-node/unattended_ssm` |
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
- [ ] **Change Operation mode** Action (`Scope.ONE`, enum dropdown, prefilled) (decision 16)
- [ ] **Change calibration** Action (`Scope.ONE`, four angle floats and three rate floats, prefilled) (decision 16)

Tests:
- [x] Config round-trip: each new key written by `update_config` survives with comments and is applied live
- [x] A Node with no new keys behaves exactly as today (angles, mode)
- [x] Named moves use the configured angle; continuous moves don't
- [ ] Both Actions: prefill reads the Node; submit sends the right payload; Node errors become a FAIL report

### Phase 2: Correlation-mode SSM (PR 2)

- [ ] In `correlation` mode, the leader and the follower move to V or D (Calibrated angles) by their own Answer, with no random bit (decision 1)
- [ ] The leader converts each question's count to a rate using the integration time it measured with, and classifies it by the nearest Reference rate: high or low → agree, medium → disagree (decisions 1, 7)
- [ ] `n_matching_bits` in correlation mode is the number of questions classified as agree; `QKDResult` keeps its shape (decision 17)
- [ ] In `correlation` mode the leader never fetches the follower's Answers (`/qkd/request_basis_list`): agreement comes only from the Coincidence levels (decisions 1, 3). Test: the follower mock fails the test if that endpoint is called.
- [ ] Entanglement mode unchanged (decision 4)
- [ ] Tests: each Answer combination moves the right angles on each side; classification at, between and beyond each reference; a game end to end with a fake time tagger in both modes; the follower's Answers are never fetched in correlation mode

### Phase 3: Unattended SSM and the digest (PR 3)

Node:
- [ ] `POST /qkd/unattended` on the leader: takes both players' Answers (any equal length), puts the follower in the follower role without asking anyone, always runs, plays the existing loop, resets coordination on both Nodes, returns the result. Entanglement: the leader's inferred bits and the follower's actual bits per question. Correlation: each question's rate and Coincidence level (decisions 11, 13)

Whobot:
- [ ] **Run SSM** Action (`Scope.ONE`, leader = chosen Node, follower = its configured follower): questions, leader and follower Answers, max error rate, integration time, all prefilled from config and the Node's mode and editable; refuses unequal-length Answers (decisions 14, 15)
- [ ] SSM report section shared by the Action and the digest: per-basis error rate (entanglement) or per-question rate vs Reference rate and expected vs measured level (correlation); OK / WARN / FAIL as in decision 13
- [ ] `whobot.toml`: `ssm_questions` (default 8), `ssm_max_error_rate` (default 0.15)
- [ ] Digest: play SSM on each Node where it is available, with that Node as leader; report each Node's Operation mode; flag a Node whose follower is in a different mode; grow the per-Node budget to cover SSM (decisions 9, 12)

Tests:
- [ ] Unattended endpoint: both modes; follower already in a game is taken over; coordination reset afterwards, also on failure
- [ ] Report statuses: OK, WARN (error rate above threshold; misclassified question), FAIL (game did not complete)
- [ ] Digest includes an SSM section per Node and the mode mismatch flag

### Phase 4: pqn-gui (own worktree, not stacked)

Worktree `/home/marcosf2/orca/workspaces/pqn-gui/operation_mode`, branch `marcosfrenkel/operation_mode`. Starts after Phase 1 is deployed somewhere it can call.

- [ ] Read `GET /node/operation_mode`
- [ ] Home screen and CHSH start-screen notes in correlation mode ([User-facing text](#user-facing-text))
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
