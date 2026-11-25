#!/usr/bin/env python3
"""Aggregate multi-model blind pairwise tournament CSV votes into order-invariant Elo,
Condorcet standings, inter-judge agreement, optional 7-dim rubric self-consistency,
and post-hoc blind self-preference audit (stdlib only)."""
import argparse
import json
import math
import random
import re
from itertools import combinations
from pathlib import Path


def parse_csv_matches(path: Path, expected_pairs):
    text = path.read_text(encoding="utf-8")
    blocks = re.findall(r"```csv\s*\n(.*?)\n```", text, flags=re.S)
    if not blocks:
        raise ValueError(f"No ```csv block found in {path}")
    raw = blocks[0].strip()
    matches = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("PAIR,"):
            continue
        parts = line.split(",", 4)
        if len(parts) < 4:
            continue
        pair_id = int(parts[0].strip())
        left = parts[1].strip().upper()
        right = parts[2].strip().upper()
        winner = parts[3].strip().upper()
        reason = parts[4].strip() if len(parts) > 4 else ""
        if winner not in ("LEFT", "RIGHT", "TIE"):
            raise ValueError(f"Invalid winner '{winner}' in {path}: {line}")
        matches.append((pair_id, left, right, winner, reason))
    if len(matches) != len(expected_pairs):
        raise ValueError(f"Expected {len(expected_pairs)} matches in {path}, got {len(matches)}")
    seen = [(m[1], m[2]) for m in matches]
    if seen != expected_pairs:
        raise ValueError(f"Pair order mismatch in {path}: expected {expected_pairs[:3]}..., got {seen[:3]}...")
    return matches


def parse_rubric_totals(path: Path, cands):
    """Parse optional Part 1 Markdown rubric score table (e.g., 7 dims + Total)."""
    cand_set = set(cands)
    totals = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s.startswith("|") or s.count("|") < 4:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells:
            continue
        cid = re.sub(r"[*`_\s]", "", cells[0]).upper()
        if cid not in cand_set or cid in totals:
            continue
        nums = []
        for cell in cells[1:]:
            cleaned = re.sub(r"[*`_\s]", "", cell)
            m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:/[0-9]+)?", cleaned)
            if m:
                nums.append(float(m.group(1)))
            elif nums:
                break
        if len(nums) >= 2:
            # Prefer explicit total column if it matches sum of preceding dims; otherwise use last numeric col.
            if abs(sum(nums[:-1]) - nums[-1]) <= 0.51:
                totals[cid] = nums[-1]
            else:
                totals[cid] = nums[-1]
    return totals if len(totals) == len(cands) else None


def elo_pass(match_list, cands, k=32.0):
    r = {c: 1500.0 for c in cands}
    for _, left, right, winner, _ in match_list:
        ea = 1.0 / (1.0 + 10.0 ** ((r[right] - r[left]) / 400.0))
        sa = 1.0 if winner == "LEFT" else (0.5 if winner == "TIE" else 0.0)
        delta = k * (sa - ea)
        r[left] += delta
        r[right] -= delta
    return r


def mc_elo(match_list, cands, n_perm=10000, seed=20261125, k=24.0):
    rng = random.Random(seed)
    acc = {c: 0.0 for c in cands}
    sq = {c: 0.0 for c in cands}
    buf = list(match_list)
    for _ in range(n_perm):
        rng.shuffle(buf)
        r = elo_pass(buf, cands, k=k)
        for c in cands:
            acc[c] += r[c]
            sq[c] += r[c] * r[c]
    mean = {c: acc[c] / n_perm for c in cands}
    std = {c: math.sqrt(max(0.0, sq[c] / n_perm - mean[c] ** 2)) for c in cands}
    return mean, std


def record_stats(match_list, cands):
    stats = {c: [0, 0, 0] for c in cands}
    for _, l, r, w, _ in match_list:
        if w == "LEFT":
            stats[l][0] += 1
            stats[r][2] += 1
        elif w == "RIGHT":
            stats[r][0] += 1
            stats[l][2] += 1
        else:
            stats[l][1] += 1
            stats[r][1] += 1
    return stats


def kendall_tau(rank_a, rank_b, pairs):
    conc = disc = 0
    for x, y in pairs:
        da = rank_a[x] - rank_a[y]
        db = rank_b[x] - rank_b[y]
        if da * db > 0:
            conc += 1
        elif da * db < 0:
            disc += 1
    return (conc - disc) / len(pairs) if pairs else 0.0


def audit_intra_consistency(rubric, matches, elo_map, pairs, margin=1.5):
    """Compare a judge's absolute rubric totals against its own pairwise votes & Elo."""
    tau = kendall_tau(rubric, elo_map, pairs)
    inversions = []
    for _, l, r, w, _ in matches:
        diff = rubric[l] - rubric[r]
        if diff >= margin and w == "RIGHT":
            inversions.append(f"{l}({rubric[l]:g})>{r}({rubric[r]:g}) but voted {r}")
        elif diff <= -margin and w == "LEFT":
            inversions.append(f"{r}({rubric[r]:g})>{l}({rubric[l]:g}) but voted {l}")
    return {"tau_score_vs_elo": round(tau, 3), "inversions": inversions}


def main():
    ap = argparse.ArgumentParser(description="Aggregate multi-model blind tournament Elo & agreement stats.")
    ap.add_argument("--judges", nargs="+", required=True, help="Judge specs as Name=path/to/judge.md")
    ap.add_argument("--cands", required=True, help="Comma-separated candidate IDs, e.g. A,B,C,D,E")
    ap.add_argument("--provenance", help="Optional JSON mapping cand -> {label, authors:[...], origin}")
    ap.add_argument("--out", help="Optional output markdown report path (also writes .json next to it)")
    ap.add_argument("--perms", type=int, default=10000, help="Monte Carlo permutations for pooled Elo")
    ap.add_argument("--seed", type=int, default=20261125, help="RNG seed")
    ap.add_argument("--inversion-margin", type=float, default=1.5, help="Rubric score gap to flag pairwise inversion")
    args = ap.parse_args()

    cands = [c.strip().upper() for c in args.cands.split(",") if c.strip()]
    expected_pairs = list(combinations(cands, 2))
    judges = {}
    for spec in args.judges:
        name, path_str = spec.split("=", 1)
        judges[name.strip()] = Path(path_str.strip())

    provenance = json.loads(Path(args.provenance).read_text(encoding="utf-8")) if args.provenance else {}
    all_matches, per_judge_elo, rubric_scores, intra_consistency, pooled = {}, {}, {}, {}, []

    for j, p in judges.items():
        ms = parse_csv_matches(p, expected_pairs)
        all_matches[j] = ms
        per_judge_elo[j], _ = mc_elo(ms, cands, n_perm=min(3000, args.perms), seed=args.seed, k=32.0)
        pooled.extend(ms)
        rub = parse_rubric_totals(p, cands)
        if rub:
            rubric_scores[j] = rub
            intra_consistency[j] = audit_intra_consistency(
                rub, ms, per_judge_elo[j], expected_pairs, margin=args.inversion_margin
            )

    pooled_mean, pooled_std = mc_elo(pooled, cands, n_perm=args.perms, seed=args.seed, k=24.0)
    pooled_rec = record_stats(pooled, cands)

    n_judges = len(judges)
    h2h = {c: {d: 0.0 for d in cands} for c in cands}
    unanimous, divergent = [], []
    for idx, (l, r) in enumerate(expected_pairs):
        votes, score_l = {}, 0.0
        for j in judges:
            _, _, _, w, reason = all_matches[j][idx]
            votes[j] = (w, reason)
            score_l += 1.0 if w == "LEFT" else (0.5 if w == "TIE" else 0.0)
        h2h[l][r] = score_l
        h2h[r][l] = float(n_judges) - score_l
        ws = {votes[j][0] for j in judges}
        if len(ws) == 1:
            unanimous.append((l, r, next(iter(ws)), votes))
        elif "LEFT" in ws and "RIGHT" in ws:
            divergent.append((l, r, score_l, votes))

    half = n_judges / 2.0
    condorcet = {
        c: (
            sum(1 for d in cands if d != c and h2h[c][d] > half),
            sum(1 for d in cands if d != c and h2h[c][d] == half),
            sum(1 for d in cands if d != c and h2h[c][d] < half),
        )
        for c in cands
    }
    order = sorted(cands, key=lambda c: (pooled_mean[c], condorcet[c][0]), reverse=True)
    jnames = list(judges.keys())
    has_all_rubrics = len(rubric_scores) == len(jnames) and len(jnames) > 0

    lines = [f"# Multi-Model Blind Tournament Summary ({len(pooled)} total matches)\n"]
    rub_hdr = " Mean Score |" if has_all_rubrics else ""
    rub_sep = "---:|" if has_all_rubrics else ""
    header = "| Rank | ID | Label |" + rub_hdr + " W-T-L | Condorcet | Pooled Elo (±σ) | " + " | ".join(f"{j} Elo" for j in jnames) + " |"
    sep = "|---:|:---:|---|" + rub_sep + "---:|---:|---:|" + "---:|" * len(jnames)
    lines.extend([header, sep])
    for rk, c in enumerate(order, 1):
        w, t, l = pooled_rec[c]
        cw, ct, cl = condorcet[c]
        lbl = provenance.get(c, {}).get("label", c)
        rub_col = f" {sum(rubric_scores[j][c] for j in jnames)/len(jnames):.1f} |" if has_all_rubrics else ""
        jcols = " | ".join(f"{per_judge_elo[j][c]:.1f}" for j in jnames)
        lines.append(f"| {rk} | **{c}** | {lbl} |{rub_col} {w}-{t}-{l} | {cw}-{ct}-{cl} | **{pooled_mean[c]:.1f}** (±{pooled_std[c]:.1f}) | {jcols} |")

    lines.append("\n## Inter-Judge Agreement\n")
    lines.append("| Pair | Exact Match | Non-Tie Agree | Kendall τ |")
    lines.append("|---|---:|---:|---:|")
    agreements = {}
    for j1, j2 in combinations(jnames, 2):
        n_p = len(expected_pairs)
        exact = sum(1 for i in range(n_p) if all_matches[j1][i][3] == all_matches[j2][i][3])
        nt = [i for i in range(n_p) if all_matches[j1][i][3] != "TIE" and all_matches[j2][i][3] != "TIE"]
        nt_ag = sum(1 for i in nt if all_matches[j1][i][3] == all_matches[j2][i][3])
        tau = kendall_tau(per_judge_elo[j1], per_judge_elo[j2], expected_pairs)
        agreements[f"{j1} vs {j2}"] = {"exact": exact, "total": n_p, "non_tie": f"{nt_ag}/{len(nt)}", "tau": round(tau, 3)}
        lines.append(f"| {j1} vs {j2} | {exact}/{n_p} ({100*exact/n_p:.1f}%) | {nt_ag}/{len(nt)} ({100*nt_ag/max(1,len(nt)):.1f}%) | {tau:+.3f} |")

    lines.append(f"\n- Unanimous pairs: {len(unanimous)}/{len(expected_pairs)} ({100*len(unanimous)/len(expected_pairs):.1f}%)")
    lines.append(f"- Divergent pairs (LEFT vs RIGHT): {len(divergent)}/{len(expected_pairs)} ({100*len(divergent)/len(expected_pairs):.1f}%)")

    if intra_consistency:
        lines.append("\n## Intra-Judge Self-Consistency (Rubric Totals vs Pairwise Elo)\n")
        lines.append(f"| Judge | Score-vs-Elo τ | Inversions (ΔScore ≥ {args.inversion_margin:g}) | Details |")
        lines.append("|---|---:|---:|---|")
        for j in jnames:
            if j not in intra_consistency:
                continue
            ic = intra_consistency[j]
            detail = "; ".join(ic["inversions"]) if ic["inversions"] else "None"
            lines.append(f"| {j} | {ic['tau_score_vs_elo']:+.3f} | {len(ic['inversions'])} | {detail} |")

    self_audit = {}
    if provenance and len(jnames) >= 2:
        lines.append("\n## Post-Hoc Blind Self-Preference Audit\n")
        lines.append("| Judge | Own Cands | Judge Mean Elo | Peers Mean Elo | Delta (Self − Peers) |")
        lines.append("|---|---|---:|---:|---:|")
        for j in jnames:
            own = [c for c in cands if j in provenance.get(c, {}).get("authors", [])]
            if not own:
                continue
            jm = sum(per_judge_elo[j][c] for c in own) / len(own)
            peers = [pj for pj in jnames if pj != j]
            pm = sum(per_judge_elo[pj][c] for pj in peers for c in own) / (len(peers) * len(own))
            self_audit[j] = {"own": own, "self_elo": round(jm, 1), "peer_elo": round(pm, 1), "delta": round(jm - pm, 1)}
            lines.append(f"| {j} | {','.join(own)} | {jm:.1f} | {pm:.1f} | {jm - pm:+.1f} |")

    report = "\n".join(lines) + "\n"
    if args.out:
        out_p = Path(args.out)
        out_p.write_text(report, encoding="utf-8")
        out_p.with_suffix(".json").write_text(json.dumps({
            "order": order, "pooled_mean": pooled_mean, "pooled_std": pooled_std,
            "pooled_rec": pooled_rec, "condorcet": condorcet,
            "per_judge_elo": per_judge_elo, "rubric_scores": rubric_scores,
            "intra_consistency": intra_consistency, "agreements": agreements, "self_audit": self_audit,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
