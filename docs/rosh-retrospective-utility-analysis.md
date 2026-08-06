# R.O.S.H. Retrospective Utility Analysis

## Scope

This is a retrospective exploratory analysis. It does not revalidate historical cutoff semantics, require official-v2 lineage, or constitute leakage-free OOS or deployment evidence. It uses only the legacy `pure_lineup_score`; player-adjusted/effective scores, Draft, Cluster, Player-Hero, and odds are excluded.

- Version: `rosh-retrospective-utility-v1`
- Formula: `dematus-rosh-0e1e6651dd932055dee69c4fb44435774f619793`
- Mode: `retrospective_exploratory`
- Series-clustered bootstrap samples: 2000
- Sanity permutations per baseline: 200
- Source unchanged: `true`

## Direction and Canonical Selection

The frozen formula defines positive `pure_lineup_score` as Radiant lineup advantage, negative as Dire lineup advantage, and `0.0` as neutral. This direction was fixed from repository code before reading outcomes and was not flipped after observing results.

Canonical rule: `earliest_source_as_of_then_source_week_then_score_key`.

| Stage | Support |
| --- | ---: |
| candidate rows | 561 |
| evidence hash valid | 561 |
| duplicate match/formula groups | 46 |
| duplicate rows | 92 |
| conflicting-score duplicate groups | 1 |
| canonical maps | 515 |
| formal valid results | 515 |
| paired Team Rating cohort | 513 |
| missing Team Rating prediction | 2 |
| standalone series clusters | 255 |
| missing series IDs (singleton match clusters) | 4 |

Scope: patch `60`; events `5`; months `2026-04, 2026-05, 2026-06, 2026-07, 2026-08`; prediction cutoffs `2026-04-18T07:00:08+00:00` through `2026-08-04T12:29:35+00:00`.

## Analysis 1: Standalone Utility

Support: **515** maps.

| Cohort | Support | Mean | Median | SD | Min | Max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| All maps | 515 | 0.549903 | 0.400000 | 12.498617 | -35.300000 | 33.100000 |
| Radiant winners | 258 | 4.119767 | 3.850000 | 12.065246 | -35.300000 | 33.100000 |
| Dire winners | 257 | -3.033852 | -3.400000 | 11.906953 | -33.700000 | 28.000000 |
| Winner-aligned | 515 | 3.577864 | 3.800000 | 11.987176 | -35.300000 | 33.700000 |

| Metric | Estimate | Series-clustered 95% CI |
| --- | ---: | ---: |
| Point-biserial correlation | 0.286454 | [0.203925, 0.369778] |
| AUC | 0.665852 | [0.614546, 0.715275] |
| Neutral-threshold accuracy | 0.629126 | [0.584939, 0.672713] |
| Radiant-win minus Dire-win mean score | 7.153620 | [4.989245, 9.407218] |

Exact-zero scores receive 0.5 credit in neutral-threshold accuracy; positive predicts Radiant and negative predicts Dire.

### Quintiles

| Bin | Support | Score min | Score max | Score mean | Radiant win rate |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 103 | -35.300000 | -9.400000 | -16.894175 | 0.330097 |
| 2 | 103 | -9.400000 | -2.400000 | -6.233981 | 0.339806 |
| 3 | 103 | -2.300000 | 3.700000 | 0.661165 | 0.563107 |
| 4 | 103 | 3.800000 | 10.400000 | 7.171845 | 0.572816 |
| 5 | 103 | 10.400000 | 33.100000 | 18.044660 | 0.699029 |

Monotonicity: Spearman rho `1.000000`; nondecreasing adjacent steps `4/4`.

### Deciles

| Bin | Support | Score min | Score max | Score mean | Radiant win rate |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 52 | -35.300000 | -16.200000 | -21.451923 | 0.230769 |
| 2 | 51 | -16.100000 | -9.400000 | -12.247059 | 0.431373 |
| 3 | 52 | -9.400000 | -6.300000 | -8.000000 | 0.326923 |
| 4 | 51 | -6.300000 | -2.400000 | -4.433333 | 0.352941 |
| 5 | 52 | -2.300000 | 0.400000 | -0.871154 | 0.500000 |
| 6 | 51 | 0.500000 | 3.700000 | 2.223529 | 0.627451 |
| 7 | 52 | 3.800000 | 7.300000 | 5.607692 | 0.538462 |
| 8 | 51 | 7.300000 | 10.400000 | 8.766667 | 0.607843 |
| 9 | 52 | 10.400000 | 16.000000 | 13.284615 | 0.692308 |
| 10 | 51 | 16.200000 | 33.100000 | 22.898039 | 0.705882 |

Monotonicity: Spearman rho `0.927273`; nondecreasing adjacent steps `7/9`.

Quantile bins are equal-count bins ordered by fixed score direction and then `match_id`; identical boundary scores may appear in adjacent bins.

### Standalone patch slices

| Slice | Support | Win rate | Score mean | Correlation | AUC | Threshold accuracy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 60 | 515 | 0.500971 | 0.549903 | 0.286454 | 0.665852 | 0.629126 |

### Standalone event slices

| Slice | Support | Win rate | Score mean | Correlation | AUC | Threshold accuracy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| blast-slam-vii-2026 | 89 | 0.483146 | -0.760674 | 0.292185 | 0.663549 | 0.640449 |
| dreamleague-s29-2026 | 184 | 0.516304 | 0.426087 | 0.285187 | 0.661502 | 0.630435 |
| ewc-dota2-2026 | 109 | 0.431193 | 1.602752 | 0.126699 | 0.570693 | 0.522936 |
| games-of-the-future-2026 | 30 | 0.566667 | 2.720000 | 0.135327 | 0.574661 | 0.633333 |
| pgl-wallachia-s8-2026 | 103 | 0.543689 | 0.157282 | 0.511017 | 0.804331 | 0.728155 |

### Standalone month slices

| Slice | Support | Win rate | Score mean | Correlation | AUC | Threshold accuracy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-04 | 103 | 0.543689 | 0.157282 | 0.511017 | 0.804331 | 0.728155 |
| 2026-05 | 246 | 0.520325 | 0.589837 | 0.265538 | 0.648669 | 0.623984 |
| 2026-06 | 27 | 0.370370 | -4.977778 | 0.402286 | 0.758824 | 0.722222 |
| 2026-07 | 119 | 0.453782 | 1.464706 | 0.144409 | 0.585328 | 0.546218 |
| 2026-08 | 20 | 0.500000 | 4.100000 | 0.053174 | 0.520000 | 0.550000 |

## Analysis 2: Incremental Utility over Team Rating

Paired support: **513** maps. Team Rating is a fixed logit offset. M1 adds exactly one coefficient on the training-fold-standardized pure R.O.S.H. score. Predictions are series-grouped five-fold out-of-fold predictions, but they remain retrospective and are not leakage-free OOS evidence.

OOF prediction hash: `428883895cefe6c73ac219119dbe928762497d9b9c8944d6531df127654b9896`.

| Model | Brier | Log loss | AUC | Accuracy |
| --- | ---: | ---: | ---: | ---: |
| M0 Team Rating-only | 0.236298 | 0.664826 | 0.632113 | 0.602339 |
| M1 Team Rating + pure R.O.S.H. | 0.215547 | 0.620386 | 0.715072 | 0.651072 |

| M1-M0 metric | Delta | Series-clustered 95% CI |
| --- | ---: | ---: |
| brier_score | -0.020751 | [-0.033011, -0.008779] |
| log_loss | -0.044440 | [-0.071798, -0.017159] |
| auc | 0.082958 | [0.038077, 0.133252] |
| accuracy | 0.048733 | [-0.001993, 0.099631] |

For Brier/log loss, negative deltas favor M1; for AUC/accuracy, positive deltas favor M1.

### Fold coefficients

| Fold | Train | Test | Train mean | Train scale | Beta |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 410 | 103 | 0.663415 | 12.533961 | 0.585744 |
| 2 | 410 | 103 | 0.427073 | 12.396849 | 0.669853 |
| 3 | 410 | 103 | 0.846829 | 12.467836 | 0.658996 |
| 4 | 411 | 102 | 0.495864 | 12.513403 | 0.732780 |
| 5 | 411 | 102 | 0.303406 | 12.506959 | 0.703901 |

### Incremental patch direction

| Slice | Support | Brier delta | Log-loss delta | AUC delta | Accuracy delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| 60 | 513 | -0.020751 | -0.044440 | 0.082958 | 0.048733 |

Direction stability:

| Metric | Favorable slices | Evaluable slices | Fraction |
| --- | ---: | ---: | ---: |
| brier_score | 1 | 1 | 1.000000 |
| log_loss | 1 | 1 | 1.000000 |
| auc | 1 | 1 | 1.000000 |
| accuracy | 1 | 1 | 1.000000 |

### Incremental event direction

| Slice | Support | Brier delta | Log-loss delta | AUC delta | Accuracy delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| blast-slam-vii-2026 | 89 | -0.021890 | -0.049250 | 0.103640 | 0.056180 |
| dreamleague-s29-2026 | 184 | -0.023626 | -0.051069 | 0.082910 | 0.043478 |
| ewc-dota2-2026 | 109 | 0.009067 | 0.019010 | -0.045642 | -0.045872 |
| games-of-the-future-2026 | 30 | 0.004145 | 0.019868 | 0.108597 | 0.033333 |
| pgl-wallachia-s8-2026 | 101 | -0.054087 | -0.115700 | 0.312451 | 0.158416 |

Direction stability:

| Metric | Favorable slices | Evaluable slices | Fraction |
| --- | ---: | ---: | ---: |
| brier_score | 3 | 5 | 0.600000 |
| log_loss | 3 | 5 | 0.600000 |
| auc | 4 | 5 | 0.800000 |
| accuracy | 4 | 5 | 0.800000 |

### Incremental month direction

| Slice | Support | Brier delta | Log-loss delta | AUC delta | Accuracy delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-04 | 101 | -0.054087 | -0.115700 | 0.312451 | 0.158416 |
| 2026-05 | 246 | -0.019697 | -0.043473 | 0.070379 | 0.024390 |
| 2026-06 | 27 | -0.053703 | -0.114281 | 0.235294 | 0.259259 |
| 2026-07 | 119 | 0.005177 | 0.011264 | -0.027350 | -0.025210 |
| 2026-08 | 20 | 0.024828 | 0.066384 | 0.060000 | -0.050000 |

Direction stability:

| Metric | Favorable slices | Evaluable slices | Fraction |
| --- | ---: | ---: | ---: |
| brier_score | 3 | 5 | 0.600000 |
| log_loss | 3 | 5 | 0.600000 |
| auc | 4 | 5 | 0.800000 |
| accuracy | 3 | 5 | 0.600000 |

## Sanity Checks

Each baseline uses 200 deterministic permutations and reruns the same grouped CV with train-fold-only standardization.

### R.O.S.H. score permutation

| Metric | Mean delta | 95% range | Favorable fraction | Empirical probability as/more favorable than observed |
| --- | ---: | ---: | ---: | ---: |
| brier_score | 0.000600 | [-0.001248, 0.002484] | 0.220000 | 0.004975 |
| log_loss | 0.001299 | [-0.002441, 0.005461] | 0.225000 | 0.004975 |
| auc | -0.002892 | [-0.013735, 0.008804] | 0.250000 | 0.004975 |
| accuracy | -0.009318 | [-0.025341, 0.005897] | 0.100000 | 0.004975 |

### Outcome-label permutation

| Metric | Mean delta | 95% range | Favorable fraction | Empirical probability as/more favorable than observed |
| --- | ---: | ---: | ---: | ---: |
| brier_score | 0.000609 | [-0.001844, 0.002899] | 0.240000 | 0.004975 |
| log_loss | 0.001557 | [-0.002974, 0.006657] | 0.185000 | 0.004975 |
| auc | -0.000102 | [-0.012136, 0.021045] | 0.390000 | 0.004975 |
| accuracy | 0.000312 | [-0.019493, 0.025341] | 0.420000 | 0.004975 |

## Conclusion

**incremental retrospective information beyond Team Rating**

This conclusion is limited to association in the saved retrospective data. The legacy audit already established that these scores were generated after prediction cutoff and lack replayable raw normalized statistics. Grouped CV cannot remove that leakage risk. The result therefore does not authorize a model change, Calibration change, Deployment freeze, production prediction, or order creation.

The complete local JSON contains every out-of-fold prediction and is intentionally kept under ignored `dogfood-output` rather than committed.
