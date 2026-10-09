"""Fraud risk analysis: where does fraud concentrate, and what rule set catches it cheaply?

Run:  python src/gen_data.py --out data/raw && python src/analysis.py
Outputs: figures/*.png and REPORT.md (every number in the report is computed here).
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIG = ROOT / "figures"
FIG.mkdir(exist_ok=True)
BLUE, RED, GREY = "#1f5fbf", "#d1342f", "#9aa3ad"

plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "font.size": 11,
                     "axes.titleweight": "bold", "axes.titlesize": 13, "figure.dpi": 130})

con = duckdb.connect()
con.execute(f"""CREATE VIEW tx AS
  SELECT t.*, CAST(t.txn_timestamp AS TIMESTAMP) ts, date_diff('day', CAST(c.signup_date AS DATE), CAST(t.txn_timestamp AS DATE)) AS acct_age_days
  FROM read_csv_auto('{(ROOT / 'data/raw/transactions.csv').as_posix()}') t
  JOIN read_csv_auto('{(ROOT / 'data/raw/customers.csv').as_posix()}') c USING (customer_id)""")
q = lambda sql: con.execute(sql).df()  # noqa: E731

overall = q("""SELECT count(*) n, sum(is_fraud) fraud, 100.0*avg(is_fraud) rate_pct,
               sum(CASE WHEN is_fraud=1 THEN amount_ngn END) fraud_value, sum(amount_ngn) total_value FROM tx""").iloc[0]
base = overall.rate_pct / 100
DAYS = int(q("SELECT date_diff('day', min(ts), max(ts)) d FROM tx").iloc[0].d)


def seg(case_sql: str) -> pd.DataFrame:
    d = q(f"SELECT {case_sql} AS seg, count(*) n, sum(is_fraud) fraud, avg(is_fraud) rate FROM tx GROUP BY 1")
    d["lift"] = d["rate"] / base
    return d


by_hour = seg("CASE WHEN hour(ts) < 5 THEN '00-04' WHEN hour(ts) < 12 THEN '05-11' WHEN hour(ts) < 18 THEN '12-17' ELSE '18-23' END")
by_amt = seg("CASE WHEN amount_ngn < 10000 THEN '<10k' WHEN amount_ngn < 50000 THEN '10k-50k' WHEN amount_ngn < 200000 THEN '50k-200k' ELSE '200k+' END")
by_age = seg("CASE WHEN acct_age_days < 14 THEN '0-13 days' WHEN acct_age_days < 90 THEN '14-89 days' ELSE '90+ days' END")
by_type = seg("txn_type").sort_values("rate", ascending=False)
order = {"hour": ["00-04", "05-11", "12-17", "18-23"], "amt": ["<10k", "10k-50k", "50k-200k", "200k+"], "age": ["0-13 days", "14-89 days", "90+ days"]}
for d, k in ((by_hour, "hour"), (by_amt, "amt"), (by_age, "age")):
    d["seg"] = pd.Categorical(d["seg"], order[k], ordered=True)
    d.sort_values("seg", inplace=True)

# ---- Figure 1: the headline - fraud rate by segment, red where lift > 2x
fig, axes = plt.subplots(1, 3, figsize=(13, 4))
for ax, d, title in zip(axes, (by_hour, by_amt, by_age), ("Time of day", "Ticket size (NGN)", "Account age")):
    ax.bar(d["seg"].astype(str), d["rate"] * 100, color=[RED if x > 2 else GREY for x in d["lift"]])
    ax.axhline(base * 100, color=BLUE, ls="--", lw=1)
    ax.set_title(title)
    for i, (r, l) in enumerate(zip(d["rate"], d["lift"])):
        ax.text(i, r * 100, f"{l:.1f}x", ha="center", va="bottom", fontsize=9)
axes[0].set_ylabel("Fraud rate (%)")
fig.suptitle("Fraud concentrates in late-night, high-value and brand-new-account activity (dashed = average)", x=0.01, ha="left", fontweight="bold")
fig.tight_layout()
fig.savefig(FIG / "01_fraud_rate_by_segment.png")
plt.close(fig)

# ---- Figure 2: by transaction type
fig, ax = plt.subplots(figsize=(7, 3.8))
ax.barh(by_type["seg"], by_type["rate"] * 100, color=[RED if x > 1.5 else GREY for x in by_type["lift"]])
ax.invert_yaxis(); ax.set_xlabel("Fraud rate (%)"); ax.set_title("Card-not-present is the riskiest rail")
fig.tight_layout(); fig.savefig(FIG / "02_fraud_by_type.png"); plt.close(fig)

# ---- Rules engine: evaluate candidate rules (precision / recall / review load)
rules = {
    "R1  amount >= 200k": "amount_ngn >= 200000",
    "R2  00-04h": "hour(ts) < 5",
    "R3  account < 14d AND amount >= 50k": "acct_age_days < 14 AND amount_ngn >= 50000",
    "R4  card_online AND amount >= 100k": "txn_type = 'card_online' AND amount_ngn >= 100000",
}
rows = []
for name, cond in rules.items():
    r = q(f"SELECT count(*) flagged, sum(is_fraud) caught FROM tx WHERE {cond}").iloc[0]
    rows.append({"rule": name, "flagged": int(r.flagged), "caught": int(r.caught),
                 "precision_pct": 100 * r.caught / max(r.flagged, 1), "recall_pct": 100 * r.caught / overall.fraud,
                 "flag_rate_pct": 100 * r.flagged / overall.n})
union_cond = " OR ".join(f"({c})" for c in rules.values())
r = q(f"SELECT count(*) flagged, sum(is_fraud) caught FROM tx WHERE {union_cond}").iloc[0]
rows.append({"rule": "ALL rules combined (OR)", "flagged": int(r.flagged), "caught": int(r.caught),
             "precision_pct": 100 * r.caught / r.flagged, "recall_pct": 100 * r.caught / overall.fraud, "flag_rate_pct": 100 * r.flagged / overall.n})
rules_df = pd.DataFrame(rows)
combined = rules_df.iloc[-1]

fig, ax = plt.subplots(figsize=(7.5, 4.2))
ax.scatter(rules_df["flag_rate_pct"], rules_df["recall_pct"], s=90, color=[BLUE] * (len(rules_df) - 1) + [RED], zorder=3)
for _, x in rules_df.iterrows():
    ax.annotate(x["rule"].split("  ")[0].replace("ALL rules combined (OR)", "Combined"), (x["flag_rate_pct"], x["recall_pct"]), xytext=(6, 6), textcoords="offset points")
ax.plot([0, 100], [0, 100], color=GREY, ls=":", lw=1)
ax.set_xlim(0, max(rules_df["flag_rate_pct"]) * 1.2); ax.set_ylim(0, 100)
ax.set_xlabel("% of all transactions sent to review"); ax.set_ylabel("% of fraud caught (recall)")
ax.set_title("Rule trade-off: fraud caught vs analyst workload")
fig.tight_layout(); fig.savefig(FIG / "03_rule_tradeoff.png"); plt.close(fig)

# ---- Report
def md(df: pd.DataFrame, fmt: dict) -> str:
    return df.to_markdown(index=False, floatfmt=".2f") if hasattr(df, "to_markdown") and _has_tab() else _simple_md(df)


def _has_tab() -> bool:
    try:
        import tabulate  # noqa: F401
        return True
    except ImportError:
        return False


def _simple_md(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        out.append("| " + " | ".join(f"{v:,.2f}" if isinstance(v, float) else f"{v:,}" if isinstance(v, int) else str(v) for v in r) + " |")
    return "\n".join(out)


def top(d: pd.DataFrame) -> pd.Series:
    return d.sort_values("lift", ascending=False).iloc[0]


h, a, g = top(by_hour), top(by_amt), top(by_age)
rules_show = rules_df.round(2)
report = f"""# Fraud analysis - findings (auto-generated from the data)

**Scope:** {int(overall.n):,} transactions, {int(overall.fraud):,} confirmed fraud ({overall.rate_pct:.2f}%), worth NGN {overall.fraud_value/1e6:,.1f}m of NGN {overall.total_value/1e6:,.0f}m processed ({100*overall.fraud_value/overall.total_value:.2f}% of value).

## Key findings
1. **Late-night is the strongest time signal.** Transactions in the `{h.seg}` window are {h.lift:.1f}x more likely to be fraud than average.
2. **Ticket size matters.** `{a.seg}` transactions run at {a.lift:.1f}x the average fraud rate.
3. **New accounts are the riskiest cohort.** `{g.seg}` accounts are {g.lift:.1f}x the average.
4. **Rail matters.** `{by_type.iloc[0].seg}` has the highest fraud rate ({by_type.iloc[0].rate*100:.2f}%).

![segments](figures/01_fraud_rate_by_segment.png)
![type](figures/02_fraud_by_type.png)

## Rule candidates
{_simple_md(rules_show)}

The combined rule set catches **{combined.recall_pct:.0f}%** of fraud while sending only **{combined.flag_rate_pct:.1f}%** of traffic to review (precision {combined.precision_pct:.1f}% vs a {overall.rate_pct:.2f}% base rate, a {combined.precision_pct/overall.rate_pct:.0f}x improvement over random review).

![tradeoff](figures/03_rule_tradeoff.png)

## Recommendation
- **High-precision, low-friction rules first:** R1 and R4 flag only {rules_df.iloc[0].flag_rate_pct:.2f}% / {rules_df.iloc[3].flag_rate_pct:.2f}% of traffic at ~{rules_df.iloc[0].precision_pct:.0f}% precision - safe to step-up authenticate (OTP) automatically rather than hard-decline.
- **Treat the night-time rule (R2) as a score feature, not a block:** it has the best recall ({rules_df.iloc[1].recall_pct:.0f}%) but only {rules_df.iloc[1].precision_pct:.1f}% precision on {rules_df.iloc[1].flag_rate_pct:.1f}% of traffic - too noisy to action alone.
- **Rules alone are not enough:** even combined they miss {100-combined.recall_pct:.0f}% of fraud, which is the business case for a model.
- Review queue sizing: the combined set implies ~{combined.flagged / DAYS:,.0f} alerts/day (over the {DAYS}-day window) - size analyst headcount accordingly.
- Next iteration: replace static thresholds with a gradient-boosted model and track precision decay weekly.

## Caveats
Synthetic data: the risk factors above were *designed into* the generator, so the point of this project is the **method** (segmentation, lift, rule evaluation, trade-off framing), not the specific numbers. The large `200k+` lift is partly circular - fraudulent amounts are inflated in the generator - so in production, validate amount rules against *pre-fraud* behaviour (e.g. deviation from the customer's own history).
"""
(ROOT / "REPORT.md").write_text(report, encoding="utf-8")
print(report)


