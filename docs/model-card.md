# Model card — predictive maintenance (LightGBM + isotonic calibration)

## What it is
LightGBM classifier predicting `P(breakdown within 7 days)` from 4 features
(`dtc_count, harsh_count, idle_ratio, km_since_service` — the exact vector
`RiskScorer` consumes; see `services/processor/.../scoring.py`). The decision is a
cost-optimal inspection list: `EV = p × C_breakdown − C_inspection`, top-K by EV > 0.

## Training data (read this first)
**Synthetic only** — 2,000 simulated vehicles / 4,000 rows from the non-leaky wear-state
generator (`ml/src/fs_ml/features.py`; labels from realized breakdowns, features strictly
before prediction time). These numbers prove the **pipeline and method**, not real-world
efficacy. Retrain on real fleet data before any production claim.

## Measured results (`ml/artifacts/metrics.json`)

| Split | Model | PR-AUC | Precision@50 | Brier | $ saved (K=50) |
|---|---|---|---|---|---|
| Temporal | LightGBM calibrated | 0.120 | 0.14 | 0.056 | $10,000 |
| Temporal | Rule (`active DTC → high`) | 0.068 | 0.08 | 0.536 | $2,500 |
| Temporal | Logistic regression | **0.171** | **0.26** | **0.047** | **$25,000** |
| By-VIN | LightGBM calibrated | 0.147 | **0.22** | 0.044 | $20,000 |
| By-VIN | Rule | 0.062 | 0.02 | 0.549 | −$5,000 |
| By-VIN | Logistic regression | 0.145 | 0.16 | 0.045 | $12,500 |

Honest reading: the model **decisively beats the operational (rule) baseline** on both
splits — that is the comparison that matters for deployment, since the rule is what runs
today. Against logistic regression it is a wash (wins by-VIN precision@K, loses temporal
PR-AUC): at 4k rows the signal is near-linear and LightGBM has no room to pull ahead.
Robustness curve (3 noise levels) and calibration data are in `metrics.json`.

## Intended use / limits
- Input: the 4-feature vector only; output: calibrated probability + reason codes
  (LightGBM `pred_contrib`/SHAP).
- Every prediction carries `model_version_id` for auditability.
- Do not use for HR/insurance decisions; driver-facing scores come from the explainable
  formula in `ml/src/fs_ml/safety.py`, not this model.
- Refresh: retrain when the fleet mix, DTC distribution, or tariff structure shifts.
