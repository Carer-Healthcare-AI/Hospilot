"""Run 1 of PPO_EXPERIMENT_PLAN. One PPO seed per invocation, sequentially.

Everything that makes a cell comparable to CEM's cell D is a **constant** here, not an argument:
the world (``BASE``, the fabrication overrides), the training seeds, the shift count and the
scoring ranges. Only the things §4 and §5 name as varying — the PPO seed and the experiment arm —
are flags. A run that could silently change the world is a run whose log describes something
else.

**Run these sequentially.** ``scale_cem.py:47-51`` records concurrent runs roughly tripling each
other's per-generation time on this machine; the same applies here, and a wall-clock figure is one
of the numbers §6 asks for.

Two ranges, two purposes, per §6.2:

* ``--score-seed-start 101``  the **selection** test. This is where the gate is evaluated,
  because 784.25 is D's selection return 782.25 plus the sigma_floor noise scale.
* ``--score-seed-start 301``  the **confirmation** test, scored ONCE, on a policy that already
  passed selection. Reported, never re-gated.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.rl.encoder import StateEncoder
from allocation.rl.evaluate import measure
from allocation.rl.ppo import compare_ppo, train_ppo
from allocation.rl.ppo_policy import PARAM_COUNT, MixedPPOPolicy, PPOWeights
from allocation.sim.calibrate import _with_base
from allocation.sim.dataset import generate
from allocation.sim.fabricated import register

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts"

#: Held fixed, exactly as ``scale_cem.py:75-77`` holds them, so the PPO arm and every CEM cell
#: face the same world. Changing one makes the whole comparison incomparable.
SEEDS = (11, 12, 13, 14, 15, 16, 17, 18)
SHIFTS = 4
BASE = 120.0

#: Cell D, from artifacts/comparison.D_672ev_pop48.log:10-24 and .confirm301.log:10-24.
D_SELECT_RETURN, D_SELECT_DELTA, D_SELECT_T, D_SELECT_NOAWARD = 782.25, 0.096, 5.08, 0.075
D_CONFIRM_RETURN, D_CONFIRM_DELTA = 785.56, 0.100

#: §6.2's pre-registered gate: D's selection return + 2, the sigma_floor noise scale.
GATE_RETURN = D_SELECT_RETURN + 2.0
GATE_NOAWARD = D_SELECT_NOAWARD

#: VALIDATION seeds — for checkpoint selection and the §4 curve. Deliberately DISJOINT from the
#: 101-200 gate: run 1 probed 101-110, which sits inside the range the verdict is decided on, so
#: selecting a checkpoint on it would have let the gate score a policy chosen partly for its
#: performance on those very seeds. Reserved bands are 11-18 (CEM fitness) and 201-204
#: (fabrication sweep, scorecard.py:50); selection is 101-200 and confirmation 301-400, leaving
#: 205-300 free.
VALIDATION = tuple(range(205, 215))

#: Measured on this config, not taken from the plan. §1 quoted 13.2 steps/episode from the
#: eps-greedy corpus; the heuristic on seeds 11-18 at 4 shifts gives 411 ER transitions over 39
#: episodes = 10.5. §6's episode arithmetic is restated against this figure in the log.
MEASURED_STEPS_PER_EPISODE = 10.5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ppo-seed", type=int, default=0,
                        help="§4 requires at least 0 and 1. D is n=1 and that is the standing "
                             "criticism; do not repeat it.")
    parser.add_argument("--experiment", choices=("A", "B"), default="A",
                        help="A masks withdraw_unplanned only (§5). B leaves all six actions.")
    parser.add_argument("--total-steps", type=int, default=296_000,
                        help="§6's interaction budget: 672 evals x 8 seeds x ~55 auctions.")
    parser.add_argument("--rollout-steps", type=int, default=2048)
    parser.add_argument("--minibatch", type=int, default=256)
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--learning-rate", type=float, default=3e-4)
    parser.add_argument("--curve-every", type=int, default=5,
                        help="§4: report return vs env-steps. Evaluated deterministically on the "
                             "VALIDATION seeds (205-214), which are disjoint from the gate's "
                             "101-200 — see the note on VALIDATION. Denser (every 2) for the "
                             "first 40 iterations, where run 1's peak was.")
    parser.add_argument("--curve-dense-until", type=int, default=40,
                        help="iterations below this probe every 2 instead of --curve-every. "
                             "Set 0 for a uniform stride, which a short run wants: the dense "
                             "band exists to catch run 1's iteration-10 peak on a 145-iteration "
                             "run, and on a 30-iteration one it just triples the probe bill.")
    parser.add_argument("--gate-final", action="store_true",
                        help="gate the FINAL checkpoint instead of the best-on-validation one. "
                             "Run 1's behaviour, kept only for reproducing it.")
    parser.add_argument("--score-seed-start", type=int, default=101,
                        help="101 = selection (gated). 301 = confirmation (scored once).")
    parser.add_argument("--score-seeds", type=int, default=100)
    parser.add_argument("--score-shifts", type=int, default=6)
    parser.add_argument("--legacy-kl-stop", action="store_true",
                        help="restore run 1/2's behaviour, where the KL stop ended the value "
                             "regression too. Only for measuring the fix against it.")
    parser.add_argument("--validation-seeds", type=int, default=len(VALIDATION),
                        help="how many of the 205-300 band to probe on. 10 gives SE ~+-68, which "
                             "is wider than any effect being looked for; 96 uses the whole band.")
    parser.add_argument("--no-select", action="store_true",
                        help="probe for OBSERVATION only — no keep-best checkpoint. Selecting on "
                             "a probe whose SE exceeds the effect picks the luckiest read.")
    parser.add_argument("--train-only", action="store_true",
                        help="stop after training. Skips the 100-seed gate, which is a "
                             "pre-registered decision and not a diagnostic.")
    parser.add_argument("--entropy-coef", type=float, default=0.01,
                        help="§4's frozen value is 0.01. Raising it keeps the categorical from "
                             "sharpening, which is the variable that couples the sampled and "
                             "deterministic returns as they converge.")
    parser.add_argument("--target-kl", type=float, default=0.02,
                        help="§4's frozen value is 0.02. The baseline runs at a measured KL of "
                             "0.024-0.030, i.e. the trust region is truncating every iteration.")
    parser.add_argument("--diverse-worlds", action="store_true",
                        help="ABLATION arm B: draw a FRESH set of world seeds from --world-pool "
                             "for every rollout instead of replaying seeds 11-18. Same count per "
                             "batch, so only across-iteration reuse changes.")
    parser.add_argument("--world-pool", type=int, nargs=2, default=(10_000, 100_000),
                        metavar=("LO", "HI"),
                        help="the band --diverse-worlds draws from. Disjoint from 11-18 "
                             "(CEM fitness), 101-200 (gate), 201-204 (fabrication sweep), "
                             "205-300 (validation) and 301-400 (confirmation).")
    parser.add_argument("--checkpoint-every", type=int, default=0,
                        help="also save er_policy.<label>.itNN.json every N iterations, so any "
                             "point on the curve can be re-scored later without re-training.")
    parser.add_argument("--label", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--eval-only", default=None,
                        help="skip training and score an existing weights file.")
    args = parser.parse_args(argv)

    label = args.label or f"{args.experiment}_s{args.ppo_seed}"
    weights_path = OUT / f"er_policy.ppo_{label}.json"
    best_path = OUT / f"er_policy.ppo_{label}.best.json"
    if weights_path.exists() and not args.force and not args.eval_only:
        print(f"REFUSING: {weights_path.name} exists. Pass --label for a new run, or --force. A "
              "run that overwrites another run's weights leaves a log describing a run the file "
              "no longer holds.")
        return 2

    log_path = OUT / f"train_ppo.{label}.log"
    log = log_path.open("a" if args.eval_only else "w", encoding="utf-8", buffering=1)

    def say(text: str = "") -> None:
        log.write(text + "\n")
        log.flush()
        print(text, flush=True)

    config = _with_base(load_config(), BASE)
    fab = register({
        "arrival.bed_release_per_hour": 1.8,
        "arrival.candidate_per_hour": 3.6,
    })
    encoder = StateEncoder()
    mask_unplanned = args.experiment == "A"

    rollouts = max(1, args.total_steps // args.rollout_steps)
    updates = rollouts * args.epochs * max(1, args.rollout_steps // args.minibatch)

    say(f"started              {datetime.now():%Y-%m-%d %H:%M:%S}")
    say(f"encoder_version      {encoder.version}")
    say(f"fabrication_version  {fab.version}")
    say(f"experiment           {args.experiment}  "
        f"({'withdraw_unplanned masked' if mask_unplanned else 'all six actions'})")
    say(f"ppo_seed             {args.ppo_seed}")
    say(f"parameters           {PARAM_COUNT}  (CEM's 161)")
    say(f"train seeds          {list(SEEDS)} x {SHIFTS} shifts")
    say(f"budget               {args.total_steps} ER env-steps = ~{rollouts} rollouts of "
        f"{args.rollout_steps}, ~{updates} parameter updates")
    say(f"episode shape        {MEASURED_STEPS_PER_EPISODE:.1f} ER steps/episode MEASURED on this "
        f"config, not the plan's 13.2 (which came from the eps-greedy corpus). So the budget is "
        f"~{args.total_steps / MEASURED_STEPS_PER_EPISODE:.0f} shift-episodes, and one sweep of "
        f"the 8 training seeds yields ~411 steps — a {args.rollout_steps}-step rollout replays "
        f"those 32 worlds ~{args.rollout_steps / 411:.1f}x. Reported because repeated worlds "
        f"inside one on-policy batch correlate the advantage estimates.")
    say(f"incumbent            CEM cell D: selection {D_SELECT_RETURN:.2f} "
        f"({D_SELECT_DELTA:+.1%}, t={D_SELECT_T:.2f}, noaward {D_SELECT_NOAWARD:.1%}); "
        f"confirmation {D_CONFIRM_RETURN:.2f} ({D_CONFIRM_DELTA:+.1%})")
    say(f"accept if            return >= {GATE_RETURN:.2f} AND noaward <= {GATE_NOAWARD:.1%} "
        f"AND abandonments == 0, evaluated on the SELECTION seeds")
    say(f"zero init            scores every action equally and centres alpha on 0.5 — the same "
        f"start CEM uses, and for train.py:277-280's reason: seeding at the heuristic makes any "
        f"improvement unattributable. The heuristic scores 671.13 on these training seeds; the "
        f"zero-init policy scores ~49.6. That gap is the climb.")
    say()

    if args.eval_only:
        weights = PPOWeights.load(args.eval_only, encoder)
        run = None
    else:
        probe_seeds = tuple(range(VALIDATION[0], VALIDATION[0] + args.validation_seeds))
        probe_baseline = _probe(config, None, AgentKind.ER, probe_seeds, fab, encoder)
        say(f"validation           seeds {probe_seeds[0]}-{probe_seeds[-1]} x 4 shifts, "
            f"deterministic; heuristic scores {probe_baseline:.2f} there")
        say(f"                     DISJOINT from the 101-200 gate. Run 1 probed 101-110, inside "
            f"the gate range; selecting a checkpoint on that would leak.")
        if args.no_select:
            say(f"checkpointing        FINAL only. The probe is recorded for OBSERVATION and "
                f"selects nothing: with {len(probe_seeds)} seeds its standard error is "
                f"~{68 * (10 / len(probe_seeds)) ** 0.5:.0f}, and keep-best on a probe that noisy "
                f"returns the luckiest read rather than the best policy.")
        else:
            say(f"checkpointing        keep-best on validation, plus the final. The gate scores "
                f"the BEST unless --gate-final. Run 1 had no selection at all, so its "
                f"iteration-10 policy — 716.73 on seed 0, 753.76 on seed 1, both above the "
                f"heuristic — was overwritten and is unrecoverable.")
        if args.diverse_worlds:
            say(f"training worlds      ABLATION B — {len(SEEDS)} FRESH seeds per rollout from "
                f"{args.world_pool[0]}-{args.world_pool[1]}, so ~{rollouts * len(SEEDS)} distinct "
                f"world seeds across the run instead of {len(SEEDS)}. Same seeds-per-batch, so "
                f"the ~5x within-batch replay is unchanged and only ACROSS-iteration reuse "
                f"differs.")
        else:
            say(f"training worlds      ABLATION A — seeds {list(SEEDS)} replayed every rollout: "
                f"{len(SEEDS) * SHIFTS} worlds seen ~{rollouts * args.rollout_steps // 411} times "
                f"across the run.")
        say(f"sweep knobs          entropy_coef {args.entropy_coef}  lr {args.learning_rate}  "
            f"target_kl {args.target_kl}   (§4 froze 0.01 / 0.0003 / 0.02)")
        say(f"kl stop              {'LEGACY — ends the value regression too (run 1/2 behaviour)' if args.legacy_kl_stop else 'freezes the POLICY only; the critic keeps training'}")
        say()
        say(f"  {'it':>3}  {'steps':>7}  {'ep_ret':>8}  {'loss':>9}  {'pol':>9}  {'val':>7}  "
            f"{'ent':>5}  {'kl':>7}  {'clip':>5}  {'|g|':>7}  {'ep_p':>4}  {'ep_v':>4}  "
            f"{'EV':>6}  {'EVfit':>6}  {'corr':>6}  {'sdV':>5}  {'sdR':>5}  {'ab':>2}  "
            f"{'probe':>8}  {'noawd':>6}  {'wdalt':>7}  {'winnow':>7}  time")
        started = time.time()

        # train_ppo calls the probe itself, immediately BEFORE on_iteration, so the log reads
        # the value it already computed. Re-evaluating here would double the validation cost of
        # every probe, and on a denser stride that is the difference between 10 and 20 minutes.
        probe_log: list = []
        shown = [0]

        def probe_fn(w: PPOWeights) -> float:
            # `measure` runs exactly the same generate() calls `_probe` did and additionally
            # tallies behaviour, so no-award / withdraw_alternative / win_now come free. Keep-best
            # still selects on the return alone — the behaviour columns are observation.
            metrics = measure(config, "probe", probe_seeds, 4, fab, AgentKind.ER,
                              MixedPPOPolicy(config, w, AgentKind.ER, encoder,
                                             deterministic=True, mask_unplanned=mask_unplanned),
                              encoder)
            probe_log.append(metrics)
            return metrics.discounted_return

        def on_iteration(it, live: PPOWeights) -> None:
            if args.checkpoint_every and it.index % args.checkpoint_every == 0:
                live.save(OUT / f"er_policy.ppo_{label}.it{it.index:03d}.json")
            probe = " " * 42
            if len(probe_log) > shown[0]:
                shown[0] = len(probe_log)
                m = probe_log[-1]
                probe = (f"{m.discounted_return:>8.2f}  {m.unallocated:>6.1%}  "
                         f"{m.action_mix.get('withdraw_alternative', 0.0):>7.1%}  "
                         f"{m.action_mix.get('win_now', 0.0):>7.1%}")
            say(f"  {it.index:>3}  {it.cumulative_steps:>7}  {it.mean_episode_return:>8.2f}  "
                f"{it.loss:>9.3f}  {it.policy_loss:>9.3f}  {it.value_loss:>7.4f}  "
                f"{it.entropy:>5.3f}  {it.approx_kl:>7.4f}  {it.clip_fraction:>5.1%}  "
                f"{it.grad_norm:>7.4f}  {it.epochs_run_policy:>4}  {it.epochs_run_value:>4}  "
                f"{it.ev:>+6.3f}  {it.ev_fitted:>+6.3f}  {it.corr_v_return:>+6.3f}  "
                f"{it.sd_v:>5.3f}  {it.sd_return:>5.3f}  {it.abandonments:>2}  "
                f"{probe}  {datetime.now():%H:%M:%S}")

        run = train_ppo(
            config, agent=AgentKind.ER, seeds=SEEDS, shifts=SHIFTS,
            rollout_steps=args.rollout_steps, total_steps=args.total_steps,
            minibatch=args.minibatch, epochs=args.epochs,
            learning_rate=args.learning_rate, ppo_seed=args.ppo_seed, fab=fab,
            entropy_coef=args.entropy_coef, target_kl=args.target_kl,
            mask_unplanned=mask_unplanned,
            checkpoint=str(OUT / f"er_policy.ppo_{label}.final.json"),
            best_checkpoint=None if args.no_select else str(best_path),
            kl_stop_freezes_value=args.legacy_kl_stop,
            world_pool=tuple(args.world_pool) if args.diverse_worlds else None,
            probe=probe_fn,
            probe_every=args.curve_every,
            probe_dense_until=args.curve_dense_until,
            on_iteration=on_iteration,
        )
        run.weights.save(weights_path)
        # Model selection: the gate scores the checkpoint chosen on VALIDATION, which is what
        # §6.2's accept decision is supposed to adjudicate. The final weights are saved either
        # way so the decay from best to final stays inspectable.
        if args.gate_final or not best_path.exists():
            weights = run.weights
            gated = "FINAL"
        else:
            weights = PPOWeights.load(best_path, encoder)
            gated = f"BEST-on-validation (iteration {run.best_iteration})"
        say()
        say(f"wall clock           {(time.time() - started) / 60:.1f} min")
        say(f"weights (final)      {weights_path}")
        if best_path.exists():
            say(f"weights (best)       {best_path}")
        say(f"gating               {gated}")
        say()
        say(run.report())

    if args.train_only:
        say()
        say("--train-only: the 100-seed gate was NOT run. §6.2's accept decision is "
            "pre-registered and is not something a diagnostic gets to spend.")
        say(f"finished             {datetime.now():%Y-%m-%d %H:%M:%S}")
        return 0

    # -- the comparison ----------------------------------------------------------------
    score_seeds = tuple(range(args.score_seed_start,
                              args.score_seed_start + args.score_seeds))
    kind = "SELECTION (gated)" if args.score_seed_start == 101 else "CONFIRMATION (scored once)"
    say()
    say("=" * 86)
    say(f"{kind} — seeds {score_seeds[0]}-{score_seeds[-1]}, {args.score_shifts} shifts each")
    say("=" * 86)
    comparison = compare_ppo(config, weights, AgentKind.ER, score_seeds, args.score_shifts,
                             fab, mask_unplanned)
    say(comparison.report())

    learned = comparison.learned
    passes = (learned.discounted_return >= GATE_RETURN
              and learned.unallocated <= GATE_NOAWARD
              and learned.abandonments == 0)
    say()
    if args.score_seed_start == 101:
        rising = run.rising() if run is not None else False
        if passes:
            verdict = "ACCEPT — clears the pre-registered gate"
        elif rising:
            verdict = ("INCONCLUSIVE — budget-limited. The return curve had not flattened when "
                       "the interaction budget ran out, so this is NOT evidence against PPO "
                       "(§4). Re-run with a larger --total-steps before drawing any conclusion.")
        elif run is not None and run.overfitting():
            verdict = ("REJECT — OVERFITTING. The training return climbed while the held-out "
                       "probe fell, so this is not a budget problem: more steps make it worse. "
                       "The 8 training seeds x 4 shifts give 32 worlds, and a 2048-step rollout "
                       "replays them ~5x, so each world is seen ~700 times across the run. The "
                       "remedy is world diversity, not budget.")
        else:
            verdict = ("REJECT — the curve flattened below the gate. This is a finding about PPO "
                       "at this budget on this simulator, and is reported as one.")
        say(f"gate  return {learned.discounted_return:.2f} vs {GATE_RETURN:.2f} | "
            f"noaward {learned.unallocated:.1%} vs {GATE_NOAWARD:.1%} | "
            f"aband {learned.abandonments} vs 0")
        say(f"VERDICT  {verdict}")
    else:
        say("Reported, not re-gated (§6.2). D's equivalent confirmation bar is "
            f"{D_CONFIRM_RETURN + 2.0:.2f}.")

    say()
    say("Whatever this says, it is a statement about a simulator whose outcome model is invented "
        "(sim/outcomes.py:1-8) and whose reward table has never been fitted (reward.yaml:16-18). "
        "PPO_EXPERIMENT_PLAN §10: this answers which policy paces better, never which policy "
        "saves more patients.")
    say(f"finished             {datetime.now():%Y-%m-%d %H:%M:%S}")
    return 0


def _probe(config, weights, agent, seeds, fab, encoder, mask_unplanned: bool = True) -> float:
    """Mean ER discounted return, deterministic, on a held-out probe. ``weights=None`` = heuristic."""
    policy = None
    if weights is not None:
        policy = MixedPPOPolicy(config, weights, agent, encoder, deterministic=True,
                                mask_unplanned=mask_unplanned)
    returns: list[float] = []
    for seed in seeds:
        dataset = generate(config, seed=seed, shifts=4, policy=policy, fab=fab, encoder=encoder)
        returns += [e.discounted_return for e in dataset.complete_episodes if e.agent is agent]
    return statistics.fmean(returns) if returns else 0.0


if __name__ == "__main__":
    raise SystemExit(main())
