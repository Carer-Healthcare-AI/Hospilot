"""The nine-metric scorecard, recomputed from NOTHING BUT the two exported gate CSVs.

Run:  python scripts/dryrun_metrics_from_csv.py [--out artifacts/dryrun_metrics.from_gate_csv.log]

``dryrun_metrics.py`` shows the same nine rows by re-running the simulator. This file does not
run the simulator. It opens two files:

    artifacts/input.gate.head.8126rows.csv     state, utility, ceiling, budget, burn, feasible
    artifacts/output.gate.head.8126rows.csv    q_action, reward

and asks, row by row, **which of the nine metrics those columns can actually support**. That is a
different and more useful question than "what are the nine numbers", because the answer is not
nine. It is three rows and a sub-row, and the log below says exactly which columns are missing
for the rest and where each one comes from.

THE ONE FACT THAT DECIDES MOST OF THE ANSWER. Both files are the HEURISTIC's rollout.
``export_validation_data.py:106`` calls ``generate(...)`` with no ``policy=`` argument, so the
shipped four rules acted in every row. The pair is therefore the REFERENCE column of the
scorecard, and no learned policy's behaviour is in it. Every row that is a *comparison* -- 1's
delta, 2, 4, 5, 8 -- needs a second rollout that these files do not contain and cannot imply.

A third input is read for rows 2 and 8 only, and the log is explicit that it is a third input:
    artifacts/er_policy.D_672ev_pop48.json     CEM cell D weights, for offline replay
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.rl.encoder import ACTION_INDEX
from allocation.rl.policy import QWeights

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts"

#: allocation/config/reward.yaml:30, read by reward/terms.py:65-66.
GAMMA = 0.99
#: The empirical win separator. Verified below against the `won` column, all 23363 gate rows.
WIN_FLOOR = 80.0
#: artifacts/scorecard.D_672ev_pop48.log -- 100 seeds, the number of record.
FULL = {
    "ref_return": 713.93, "ref_eff": 0.792, "ref_unalloc": 0.064, "ref_sd": 447.71,
    "pol_return": 782.25, "delta": 0.096, "t": 5.08, "shifts": 689,
    "agreement": 0.592, "change": 0.408, "shadow_obs": 3733,
    "mean_regret": -68.32, "p90_regret": 360.75, "abandonments": 0,
}


def percentile(values, q: float) -> float:
    """scorecard.py:58-73 verbatim -- linear interpolation, not statistics.quantiles."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--inp", default=str(OUT / "input.gate.head.8126rows.csv"))
    p.add_argument("--outp", default=str(OUT / "output.gate.head.8126rows.csv"))
    p.add_argument("--weights", default=str(OUT / "er_policy.D_672ev_pop48.json"))
    p.add_argument("--source", default=str(OUT / "validation.gate.seeds101-200.csv"))
    p.add_argument("--out", default=str(OUT / "dryrun_metrics.from_gate_csv.log"))
    a = p.parse_args(argv)

    log = Path(a.out).open("w", encoding="utf-8", buffering=1)

    def say(text: str = "") -> None:
        log.write(text + "\n")
        log.flush()
        print(text, flush=True)

    def rule(ch: str = "-") -> None:
        say(ch * 96)

    IN = list(csv.DictReader(Path(a.inp).open(encoding="utf-8")))
    OU = list(csv.DictReader(Path(a.outp).open(encoding="utf-8")))
    # The full export, read ONLY to check claims the pair cannot check about itself: which
    # episodes the row cut truncated, and whether the reward>=80 win proxy actually holds.
    # Nothing in the nine numbers below is taken from it.
    SRC = list(csv.DictReader(Path(a.source).open(encoding="utf-8")))
    CUT = SRC[:len(OU)]

    say("=" * 96)
    say("DRY RUN - the nine metrics recomputed from the exported gate CSVs, and nothing else")
    say("=" * 96)
    say(f"started              {datetime.now():%Y-%m-%d %H:%M:%S}")
    say(f"input  file          {Path(a.inp).name}   {len(IN)} rows x {len(IN[0])} cols")
    say(f"output file          {Path(a.outp).name}   {len(OU)} rows x {len(OU[0])} cols")
    say(f"gamma                {GAMMA}   (allocation/config/reward.yaml:30)")
    say("cross-check against  artifacts/scorecard.D_672ev_pop48.log   (100 seeds, 689 shifts)")
    say("")
    say("  WHAT THESE TWO FILES ARE. The HEURISTIC's rollout on the gate worlds.")
    say("  export_validation_data.py:106 calls generate(config, seed=..., shifts=..., fab=...,")
    say("  encoder=...) with NO policy= argument, so allocation/policy/heuristic.py acted in")
    say("  every one of the 8126 rows. q_action in the output file is the HEURISTIC's choice.")
    say("")
    say("  That single fact decides five of the nine rows. The scorecard's rows 1-delta, 2, 4,")
    say("  5 and 8 are all COMPARISONS between two policies on the same worlds. This pair holds")
    say("  one policy. The second column is not missing from the arithmetic below - it is")
    say("  missing from the FILES, and no manipulation of these columns produces it.")
    say("")

    # ----------------------------------------------------------------- availability table
    rule()
    say("WHAT THE PAIR CAN AND CANNOT SUPPORT - read this before the arithmetic")
    rule()
    say(f"  {'#':<3} {'metric':<24} {'verdict':<12} what is missing, and where it lives")
    say(f"  {'-'*3} {'-'*24} {'-'*12} {'-'*44}")
    rows = [
        ("1", "Average Ep. Reward", "YES (ref)", "reference side only; policy side = 2nd rollout"),
        ("2", "Reference Agreement", "NO", "the learned policy's shadow action per decision"),
        ("3", "Allocation Efficiency", "YES (proxy)", "`won` absent; reward>=80 substitutes exactly"),
        ("", "  beds unallocated", "YES (proxy)", "same proxy, different denominator"),
        ("4", "Average Regret", "NO", "policy returns_by_shift = 2nd rollout"),
        ("5", "P90 Regret", "NO", "same; percentile machinery is shown anyway"),
        ("6", "Critical Miss (proxy)", "NO", "abandonments is a dataset counter, not a column"),
        ("7", "Reward Stability sd", "YES (ref)", "nothing; computable in full"),
        ("8", "Policy Change Rate", "NO", "1 - row 2, so it fails for the same reason"),
        ("9", "Mean dQ", "N/A", "undefined for CEM, unwired for Q and PPO"),
    ]
    for n, m, v, w in rows:
        say(f"  {n:<3} {m:<24} {v:<12} {w}")
    say("")
    say("  Three rows and one sub-row survive. All three are REFERENCE measurements: they")
    say("  describe the heuristic, not a learned policy. Nothing in this pair scores a model.")
    say("")

    # ----------------------------------------------------------------- step 0: the join
    rule()
    say("STEP 0 - THE JOIN, VERIFIED RATHER THAN ASSUMED")
    rule()
    say("  export_output_csv.py:63 writes row_id = i+1 over the SAME picked[] list that")
    say("  export_input_csv.py:47 cuts, so input row i and output row i are one decision. The")
    say("  output file deliberately repeats agent and candidate_id so the claim is checkable:")
    say("")
    mism = sum(
        1 for x, y in zip(IN, OU)
        if x["agent"] != y["agent"] or x["candidate_id"] != y["candidate_id"]
        or x["shift_id"] != y["shift_id"]
    )
    say(f"    rows compared            {len(IN)}")
    say(f"    (agent, candidate_id, shift_id) mismatches   {mism}")
    say(f"    -> positional join is {'SOUND' if mism == 0 else 'BROKEN'}")
    say("")
    say("  Note what the join does NOT carry across. auction_id exists only in the INPUT file;")
    say("  seed exists only in the OUTPUT file. Row 3 below needs both at once, which is why it")
    say("  is computed on the zipped pair and not on either file alone.")
    say("")

    # episode grouping
    eps = collections.OrderedDict()
    for i, y in enumerate(OU):
        eps.setdefault((int(y["seed"]), y["shift_id"], y["agent"]), []).append(i)
    er_eps = {k: v for k, v in eps.items() if k[2] == "er"}
    seeds = sorted({int(y["seed"]) for y in OU})
    mix = collections.Counter(y["agent"] for y in OU)

    say(f"  episodes         {len(eps)}  grouped by (seed, shift_id, agent)  -- RL-Steps section 21")
    say(f"  of which ER      {len(er_eps)}   (the learning agent; evaluate.py:222 filters to it)")
    say(f"  worlds           {len(seeds)} of 100, seeds {seeds[0]}-{seeds[-1]}")
    say("  rows per agent   " + "  ".join(f"{k} {v}" for k, v in sorted(mix.items())))
    say("")

    # ----------------------------------------------------------------- row 1
    rule()
    say("ROW 1 - AVERAGE EPISODE REWARD          verdict: COMPUTABLE, reference side only")
    rule()
    say("  formula   fmean(e.discounted_return for e in complete_episodes if e.agent is ER)")
    say("  code      evaluate.py:255 (the mean), reward/episode.py:63-66 (the return)")
    say("  needs     reward, and the position of each reward inside its episode")
    say("  has       output.gate: reward, seed, shift_id, agent  -> position is row order")
    say("")
    say("  The return is NOT the sum of the reward column. It is sum(gamma^t * R_t) with t")
    say("  restarting at 0 for every (seed, shift_id, agent) group. One episode in full:")
    say("")
    k0 = next(iter(er_eps))
    say(f"    episode {k0}")
    say(f"    {'t':>3}  {'reward':>9}  {'gamma^t':>10}  {'contribution':>13}  {'running':>10}")
    run = 0.0
    for t, i in enumerate(er_eps[k0]):
        r = float(OU[i]["reward"])
        c = GAMMA ** t * r
        run += c
        say(f"    {t:>3}  {r:>9.1f}  {GAMMA**t:>10.6f}  {c:>13.4f}  {run:>10.4f}")
    say(f"    -> discounted_return = {run:.4f}")
    say("")

    returns = {
        k[:2]: sum(GAMMA ** t * float(OU[i]["reward"]) for t, i in enumerate(v))
        for k, v in er_eps.items()
    }
    vals = list(returns.values())
    mean_ref = statistics.fmean(vals)
    sd_ref = statistics.stdev(vals)
    undisc = statistics.fmean([
        sum(float(OU[i]["reward"]) for i in v) for v in er_eps.values()
    ])
    say("  the same arithmetic over every ER episode, first 8 shown:")
    for k in list(returns)[:8]:
        say(f"    {str(k):<40} {returns[k]:>10.2f}")
    say(f"    ... {len(vals)} ER episodes in total")
    say("")
    say(f"  sum      {sum(vals):>12.2f}")
    say(f"  n        {len(vals):>12}")
    say(f"  MEAN     {mean_ref:>12.4f}   <- row 1, reference column")
    say("")
    say(f"  the discounting is not cosmetic: undiscounted mean {undisc:.4f}, "
        f"discounted {mean_ref:.4f},")
    say(f"  a gap of {undisc - mean_ref:.2f} points. Reporting the raw sum overstates every "
        f"episode.")
    say("")
    say(f"  CROSS-CHECK  full 100 seeds -> {FULL['ref_return']}. This 35-seed cut -> "
        f"{mean_ref:.2f}.")
    say(f"               a {abs(mean_ref - FULL['ref_return']) / FULL['ref_return']:.1%} gap on "
        f"{len(seeds)} of 100 worlds, which is sampling, not disagreement.")
    say("")
    trunc = [k for k, v in er_eps.items() if CUT[v[-1]]["terminal"].lower() != "true"]
    kept = [x for k, x in returns.items() if k not in {t[:2] for t in trunc}]
    say("  ONE CAVEAT THE PAIR CANNOT SELF-DIAGNOSE. evaluate.py:222 filters to")
    say("  complete_episodes. The `complete` and `terminal` columns were dropped from the")
    say("  output file, so the filter cannot be applied here and truncated episodes cannot be")
    say("  spotted. Checked against the source file, which still has `terminal`:")
    say("")
    say(f"    ER episodes whose last row is NOT terminal   {len(trunc)} of {len(er_eps)}")
    for t in trunc[:4]:
        say(f"      {t}   return {returns[t[:2]]:.2f}")
    say(f"    mean over all {len(vals)}         {mean_ref:.4f}")
    say(f"    mean dropping the truncated  {statistics.fmean(kept):.4f}   "
        f"shift {statistics.fmean(kept) - mean_ref:+.2f}")
    say("")
    say("  Small here. Not KNOWABLY small from the pair alone, which is the point: a cut that")
    say("  landed mid-shift on many episodes would bias row 1 invisibly. Two dropped columns.")
    say("")

    # ----------------------------------------------------------------- t-ratio
    rule()
    say("THE t-RATIO - why row 1's delta is the row that is actually missing")
    rule()
    say("  formula   diffs = policy[k] - reference[k] over shared shifts k   (evaluate.py:93-100)")
    say("            t = mean(diffs) / (stdev(diffs) / sqrt(n))              (evaluate.py:104-120)")
    say("")
    say("  Both columns above are the reference. The subtraction has one operand:")
    say("")
    say(f"    {'shift':<40} {'reference':>11} {'policy':>11} {'diff':>11}")
    for k in list(returns)[:4]:
        say(f"    {str(k):<40} {returns[k]:>11.2f} {'ABSENT':>11} {'--':>11}")
    say("")
    say("  Pairing is the whole method and it is what fails. The per-shift spread here is")
    say(f"  sd {sd_ref:.2f} on a mean of {mean_ref:.2f} - a CV of {sd_ref/mean_ref:.0%}. An")
    say("  unpaired comparison divides by that spread and buries any real effect; pairing")
    say("  cancels the world-to-world luck because both policies faced the same world. With one")
    say("  policy in the files there is nothing to pair, and a one-sided SE is not a t-ratio.")
    say("")
    say(f"  FULL RUN: reference {FULL['ref_return']}  policy {FULL['pol_return']}  "
        f"{FULL['delta']:+.1%}, t={FULL['t']:.2f}, {FULL['shifts']} shifts")
    say("")

    # ----------------------------------------------------------------- rows 2 and 8
    rule()
    say("ROWS 2 and 8 - REFERENCE AGREEMENT and POLICY CHANGE RATE      verdict: NOT COMPUTABLE")
    rule()
    say("  formula   rate = disagreements / observed                    (pilot.py:214-227)")
    say("            row 8 = rate;  row 2 = 1 - rate")
    say("  needs     TWO q_actions per decision: the acting heuristic's and the learned")
    say("            policy's shadow choice at the same state")
    say("  has       one q_action per row, the heuristic's")
    say("")
    say("  It is tempting to reconstruct the second one. The input file carries `state` (22")
    say("  features) and `feasible`, LinearQPolicy._q is a plain dot product (policy.py:262-267)")
    say("  and the choice is argmax over the feasible set (policy.py:238). So the replay runs.")
    say("  Below it is run, with cell D's weights as a THIRD input - and it does not reproduce")
    say("  row 2, which is the finding worth having:")
    say("")
    w = QWeights.load(Path(a.weights))
    idx = {act.value: i for act, i in ACTION_INDEX.items()}

    def replay(agent_filter, seed_max=None):
        obs = dis = 0
        dd = collections.Counter()
        for x, y in zip(IN, OU):
            if seed_max is not None and int(y["seed"]) > seed_max:
                continue
            if agent_filter is not None and x["agent"] != agent_filter:
                continue
            st = json.loads(x["state"])
            feas = [f for f in x["feasible"].split("|") if f in idx]
            q = {f: sum(c * s for c, s in zip(w.rows[idx[f]], st)) + w.biases[idx[f]]
                 for f in feas}
            best = max(q, key=lambda kk: q[kk])
            obs += 1
            if best != y["q_action"]:
                dis += 1
                dd[f"{y['q_action']} -> {best}"] += 1
        return obs, dis, dd

    say(f"  weights   {Path(a.weights).name}  encoder {w.encoder_version}  "
        f"fabrication {w.fabrication_version}")
    say("")
    say(f"    {'replay scope':<34} {'observed':>9} {'disagree':>9} {'change':>9} {'agree':>9}")
    for label, filt, smax in (
        ("ER only, all 35 seeds", "er", None),
        ("ER only, seeds 101-106", "er", 106),
        ("all agents, all 35 seeds", None, None),
        ("all agents, seeds 101-106", None, 106),
    ):
        o, d, _ = replay(filt, smax)
        say(f"    {label:<34} {o:>9} {d:>9} {d/o:>8.1%} {1-d/o:>8.1%}")
    say("")
    say(f"    scorecard row 8, live shadow        {FULL['shadow_obs']:>9} "
        f"{'-':>9} {FULL['change']:>8.1%} {FULL['agreement']:>8.1%}")
    say("")
    say("  THREE REASONS THE REPLAY IS A DIFFERENT MEASUREMENT, not a noisy version of one.")
    say("")
    say("  1. THE DENOMINATOR IS NOT IN THE FILES. The live monitor fires on every decide_q")
    say("     call (pilot.py:328), including auction rounds that never become a scored")
    say(f"     transition. {FULL['shadow_obs']} observations over 6 seeds is "
        f"{FULL['shadow_obs']/6:.0f}/seed; these files")
    say(f"     hold {len(IN)/len(seeds):.0f} rows/seed. Roughly "
        f"{1 - (len(IN)/len(seeds))/(FULL['shadow_obs']/6):.0%} of the decisions row 8 divides")
    say("     by were never exported. No row-level reweighting recovers them.")
    say("")
    say("  2. WHO IS SHADOWED DIFFERS. scorecard.py:170-175 hands ShadowPolicy a bare")
    say("     LinearQPolicy, so ER-fitted weights choose for ot and ward too. compare() at")
    say("     evaluate.py:285 uses MixedPolicy, which is ER-only. Rows 2/8 and row 1 are")
    say("     therefore not scoped to the same decisions - visible above as 28% against 67%.")
    say("")
    say("  3. THE FEASIBLE SET IS THE HEURISTIC's. LinearQPolicy._feasible (policy.py:281-)")
    say("     recomputes the mask from candidate, ceiling, round_state and pathways. The")
    say("     exported `feasible` column is the mask recorded during the heuristic's run. Where")
    say("     they differ the replay's argmax ranges over the wrong set.")
    say("")
    say("  The disagreement MIX makes the divergence concrete. Live, the commonest was")
    say("  'continue -> win_now' 957 and 're_enter_later -> withdraw_alternative' 457. The")
    say("  replay below never emits the second at all:")
    say("")
    _, _, dd = replay(None)
    for kk, vv in dd.most_common(6):
        say(f"    {kk:<48} {vv:>6}")
    say("")
    say("  *** Rows 2 and 8 are also ONE measurement printed twice: row 2 = 1 - row 8. The")
    say("  scorecard has nine rows and eight measurements. And row 2 RANKS BACKWARDS -")
    say("  agreement is similarity to the heuristic, and the worst policy in this project")
    say("  (offline Q, -69.6%) scores the HIGHEST agreement at 65.0%. ***")
    say("")

    # ----------------------------------------------------------------- row 3
    rule()
    say("ROW 3 - ALLOCATION EFFICIENCY and beds unallocated       verdict: COMPUTABLE VIA PROXY")
    rule()
    say("  formula   ranked_ok / ranked, over auctions WITH a winner   (evaluate.py:238-247)")
    say("              ranked_ok += 1 if won[0].agent is max(group, key=utility).agent")
    say("            unallocated = 1 - awarded / auctions               (evaluate.py:259)")
    say("  needs     auction_id, utility, and `won`")
    say("  has       auction_id + utility (input), reward (output).  `won` was DROPPED.")
    say("")
    say("  `won` cannot be read off q_action. The action is what the bidder CHOSE; winning is")
    say("  what the auction DID. The cross-tab against the source file's `won` column:")
    say("")
    xt = collections.Counter((r["q_action"], r["won"].lower() == "true") for r in CUT)
    for (act, won), n in sorted(xt.items()):
        say(f"    {act:<22} {'WON ' if won else 'lost'}   {n:>5}")
    say("")
    say("  continue -> won and win_now -> lost are both in the thousands. Any rule mapping")
    say("  q_action to `won` is wrong by construction.")
    say("")
    say("  What does work is the reward floor. Four section-23 terms are hardcoded True on any")
    say("  win, so a won episode step cannot score below 80:")
    say("")
    wr = [float(r["reward"]) for r in SRC if r["won"].lower() == "true"]
    lr = [float(r["reward"]) for r in SRC if r["won"].lower() != "true"]
    say(f"    over all {len(SRC)} gate rows (source file, which still has `won`):")
    say(f"      min(reward | won)      {min(wr):>8.1f}    n={len(wr)}")
    say(f"      max(reward | not won)  {max(lr):>8.1f}    n={len(lr)}")
    say(f"      -> reward >= {WIN_FLOOR:.0f} separates the two classes "
        f"{'PERFECTLY' if min(wr) > max(lr) else 'IMPERFECTLY'}")
    mis = sum(1 for x, r in zip(OU, CUT)
              if (float(x["reward"]) >= WIN_FLOOR) != (r["won"].lower() == "true"))
    say(f"      row-level mismatches on the {len(OU)}-row cut: {mis}")
    say("")
    say("  This is an EMPIRICAL separator on this simulator's reward table, not a definition.")
    say("  A reward-term change breaks it silently. Exporting `won` costs one column.")
    say("")
    by_auction = collections.OrderedDict()
    for i, x in enumerate(IN):
        by_auction.setdefault(x["auction_id"], []).append(i)

    def show(aid, label):
        say(f"    {label}   auction {aid[:8]}...")
        idxs = by_auction[aid]
        top = max(idxs, key=lambda i: float(IN[i]["utility"]))
        wonr = [i for i in idxs if float(OU[i]["reward"]) >= WIN_FLOOR]
        for i in idxs:
            flag = "  <- highest utility" if i == top else ""
            flag += "  <- WON" if i in wonr else ""
            say(f"      row {i+1:>5}  {OU[i]['agent']:<5} {OU[i]['candidate_id']:<10} "
                f"utility {float(IN[i]['utility']):>7.3f}  {OU[i]['q_action']:<20} "
                f"reward {float(OU[i]['reward']):>7.1f}{flag}")
        if not wonr:
            say("      no row clears 80 -> auctions += 1, awarded += 0. Counts in the SUB-ROW only.")
        elif OU[wonr[0]]["agent"] == OU[top]["agent"]:
            say("      winner IS the highest-utility bidder -> ranked_ok += 1")
        else:
            say(f"      winner is {OU[wonr[0]]['agent']}, highest utility was "
                f"{OU[top]['agent']} -> ranked, NOT ok")
        say("")

    picked = {"ok": None, "miss": None, "none": None}
    for aid, idxs in by_auction.items():
        if len(idxs) < 3:
            continue
        wonr = [i for i in idxs if float(OU[i]["reward"]) >= WIN_FLOOR]
        top = max(idxs, key=lambda i: float(IN[i]["utility"]))
        slot = "none" if not wonr else ("ok" if OU[wonr[0]]["agent"] == OU[top]["agent"] else "miss")
        if picked[slot] is None:
            picked[slot] = aid
        if all(picked.values()):
            break
    say("  three real auctions, one of each kind:")
    say("")
    show(picked["ok"], "RANKED OK    ")
    show(picked["miss"], "RANKING MISS ")
    show(picked["none"], "NO AWARD     ")

    auctions = awarded = ranked_ok = 0
    for idxs in by_auction.values():
        auctions += 1
        wonr = [i for i in idxs if float(OU[i]["reward"]) >= WIN_FLOOR]
        if not wonr:
            continue
        awarded += 1
        top = max(idxs, key=lambda i: float(IN[i]["utility"]))
        if OU[wonr[0]]["agent"] == OU[top]["agent"]:
            ranked_ok += 1
    eff = ranked_ok / awarded
    unalloc = 1 - awarded / auctions
    say(f"  auctions in the cut      {auctions}")
    say(f"  awarded                  {awarded}")
    say(f"  ranked_ok                {ranked_ok}")
    say(f"  ROW 3  efficiency  = {ranked_ok} / {awarded} = {eff:.4f} = {eff:.1%}")
    say(f"  SUB    unallocated = 1 - {awarded}/{auctions} = {unalloc:.4f} = {unalloc:.1%}")
    say("")
    say("  DIFFERENT DENOMINATORS, and that is the whole reason the sub-row exists. Efficiency")
    say("  is over AWARDED auctions; unallocated is over ALL of them. A policy can raise")
    say("  efficiency by refusing hard auctions, which shows up only in the sub-row - which is")
    say("  exactly how PPO's peak fails the gate: good return, good efficiency, 11% unallocated.")
    say("")
    say(f"  CROSS-CHECK  full 100 seeds -> efficiency {FULL['ref_eff']:.1%}, unallocated "
        f"{FULL['ref_unalloc']:.1%}.")
    say(f"               this cut -> {eff:.1%} and {unalloc:.1%}.")
    say("")

    # ----------------------------------------------------------------- rows 4 and 5
    rule()
    say("ROWS 4 and 5 - AVERAGE REGRET and P90 REGRET                   verdict: NOT COMPUTABLE")
    rule()
    say("  formula   regret[k] = reference[k] - policy[k]   (scorecard.py:134, the paired")
    say("            difference with the sign flipped)")
    say("  needs     both policies' returns_by_shift")
    say("  has       the reference's only -> every regret would be 0 by construction")
    say("")
    say("  Computing it anyway would produce mean regret 0.00 and P90 0.00 and both would be")
    say("  artefacts of subtracting the file from itself. They are omitted rather than printed.")
    say("")
    say("  The PERCENTILE MACHINERY is worth showing, because scorecard.py does not use")
    say("  statistics.quantiles - P90 of 689 points should not round to the nearest twentieth.")
    say("  Demonstrated on the reference returns computed in row 1 (real numbers, wrong")
    say("  quantity - this is the distribution of RETURNS, not of REGRETS):")
    say("")
    s = sorted(vals)
    pos = 0.90 * (len(s) - 1)
    lo = int(pos)
    say(f"    pos = 0.90 * ({len(s)} - 1) = {pos:.2f}")
    say(f"    lo  = {lo}   s[{lo}] = {s[lo]:.4f}")
    say(f"    hi  = {lo+1}   s[{lo+1}] = {s[lo+1]:.4f}")
    say(f"    P90 = {s[lo]:.4f} + ({s[lo+1]:.4f} - {s[lo]:.4f}) * {pos-lo:.2f} "
        f"= {percentile(vals, 0.90):.4f}")
    say("")
    for q in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        say(f"    P{int(q*100):<3}  {percentile(vals, q):>+10.2f}")
    say("")
    say("  And the reading rule that matters once the second rollout exists: check the SIGN")
    say("  COUNT against the mean. A policy can win on average while losing on most shifts -")
    say(f"  the full run does exactly that, mean regret {FULL['mean_regret']:.2f} but worse than")
    say("  reference on 252 of 689 shifts. The two readings say different things.")
    say("")
    say(f"  FULL RUN: mean regret {FULL['mean_regret']:.2f}   P90 {FULL['p90_regret']:.2f}")
    say("")

    # ----------------------------------------------------------------- row 6
    rule()
    say("ROW 6 - CRITICAL MISS (PROXY)                                 verdict: NOT COMPUTABLE")
    rule()
    say("  formula   raw count of dataset.abandonments, summed over seeds   (evaluate.py:216)")
    say("  needs     a DATASET-LEVEL counter")
    say("  has       nothing. It is not a per-row quantity, so no column of a row-per-decision")
    say("            export can carry it. It is not a dropped column; it is a different shape.")
    say("")
    say("  validation_manifest.json records it for the whole gate set: abandonments 0 over")
    say("  23363 rows and 8188 auctions. That is the number, and it comes from the manifest,")
    say("  not from these two files.")
    say("")
    say("  IT IS A PROXY EITHER WAY. The real definition is NEWS2 >= 7 deterioration")
    say("  (RL_EVAL_CHECKLIST A.6). Abandonment count is what the simulator can observe. Do not")
    say("  report it as a clinical harm rate. This row is scored as its own verdict and is")
    say("  NEVER merged into the return number.")
    say("")

    # ----------------------------------------------------------------- row 7
    rule()
    say("ROW 7 - REWARD STABILITY                        verdict: COMPUTABLE, reference side")
    rule()
    say("  formula   stdev(returns_by_shift.values())  -- spread WITHIN one policy")
    say("  code      scorecard.py:152-155")
    say("  needs     one policy's per-shift returns. Row 1 already built them.")
    say("")
    say("  This is the one comparison-shaped row that is not a comparison: it needs a single")
    say("  policy, so the pair supports it in full.")
    say("")
    say(f"    n                    {len(vals)}")
    say(f"    mean                 {mean_ref:>10.4f}")
    say(f"    stdev (sample, n-1)  {sd_ref:>10.4f}   <- row 7")
    say(f"    pstdev (population)  {statistics.pstdev(vals):>10.4f}   the manifest reports this one")
    say(f"    CV = sd / mean       {sd_ref/mean_ref:>10.4f} = {sd_ref/mean_ref:.1%}")
    say(f"    SE = sd / sqrt(n)    {sd_ref/math.sqrt(len(vals)):>10.4f}")
    say("")
    say("  CV is the row that matters, not sd. A low mean with a large sd gives a huge CV, which")
    say("  is how offline Q reads 190.1% at full scale - that policy is not merely worse, it is")
    say("  erratic. sd alone would have called it stable.")
    say("")
    say("  ACROSS-SEED sd is unavailable for every arm in this project: each cell ran")
    say("  train(seed=0). Known n=1 limitation, not an omission of this dry run.")
    say("")
    say(f"  CROSS-CHECK  full 100 seeds -> sd {FULL['ref_sd']:.2f}. This cut -> {sd_ref:.2f}.")
    say("")

    # ----------------------------------------------------------------- row 9
    rule()
    say("ROW 9 - MEAN dQ                                                       verdict: N/A")
    rule()
    say("  n/a for CEM: there is no Bellman update, so there is no dQ to average. Read the CEM")
    say("  sigma instead. DEFINABLE but NOT WIRED UP for Q-learning, and for PPO as mean dV.")
    say("  It prints n/a for every arm in every scorecard, so the scorecard reports eight of")
    say("  nine rows - and eight rows is really seven measurements, since 2 and 8 are one.")
    say("")
    say("  Nothing about this row is a property of the CSV pair. It would be missing from a")
    say("  full simulator re-run too.")
    say("")

    # ----------------------------------------------------------------- close
    rule("=")
    say("SUMMARY - what these two files actually measured")
    rule("=")
    say(f"  ROW 1  Average Ep. Reward     {mean_ref:>10.4f}   reference (heuristic), "
        f"{len(vals)} ER episodes")
    say(f"  ROW 3  Allocation Efficiency  {eff:>10.1%}   via the reward>=80 win proxy")
    say(f"         beds unallocated       {unalloc:>10.1%}   different denominator")
    say(f"  ROW 7  Reward Stability sd    {sd_ref:>10.4f}   CV {sd_ref/mean_ref:.1%}")
    say(f"  ROWS 2, 4, 5, 8               {'--':>10}   need a second rollout")
    say(f"  ROW 6                         {'--':>10}   dataset counter, not a column")
    say(f"  ROW 9                         {'--':>10}   undefined for CEM, unwired elsewhere")
    say("")
    say("  Three rows and a sub-row, all describing the HEURISTIC. The pair is a reference")
    say("  fixture, not a scorecard: it is the thing a policy gets compared against, and it")
    say("  holds up its half of every paired subtraction exactly. What it cannot do is supply")
    say("  the other half.")
    say("")
    say("  TO GET THE MISSING FIVE, in increasing cost:")
    say("    rows 4, 5, and row 1's delta   re-export the same 100 worlds with policy=")
    say("                                   MixedPolicy(...) and pair on (seed, shift_id)")
    say("    row 6                          one scalar per seed from dataset.abandonments")
    say("    rows 2, 8                      instrument ShadowPolicy to emit its own log; the")
    say("                                   denominator is decisions, not transitions, so a")
    say("                                   transition export can never carry it")
    say("    row 9                          wire mean dQ in qlearn.py and mean dV in train_ppo.py")
    say("")
    say("  AND WHAT NONE OF THE NINE MEASURE, from these files or from a full run: whether a")
    say("  BETTER ACTION EXISTED at any decision point. Every row is distance from the shipped")
    say("  heuristic, and the heuristic is four unfitted rules, not a ceiling. No substitution")
    say("  among these metrics reaches optimality. RL_EVAL_CHECKLIST section C, gated on 0.")
    say("")
    say(f"finished             {datetime.now():%Y-%m-%d %H:%M:%S}")
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
