import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from event_intelligence.draft_model import (
    CALIBRATION_MAX_LOG_LOSS,
    DraftTrainingRow,
    FeatureSchema,
    LandmarkCandidate,
    ModelStatus,
    PredictionStatus,
    evaluate_binary_predictions,
    fit_draft_model,
    passes_calibration_gate,
    predict_draft,
    select_live_landmark,
)


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
TRAINING_CUTOFF = START + timedelta(days=60)


def training_rows(count: int = 40) -> tuple[DraftTrainingRow, ...]:
    rows = []
    for index in range(count):
        tempo = float(index % 7 - 3)
        scaling = float((index * 3) % 9 - 4)
        outcome = int(tempo + 0.4 * scaling + (index % 2) > 0)
        cutoff = START + timedelta(days=index)
        duration = 25.0 + index % 15
        completed_at = cutoff + timedelta(minutes=duration)
        rows.append(
            DraftTrainingRow(
                match_id=10_000 + index,
                input_snapshot_hash=f"{index + 1:064x}",
                cutoff=cutoff,
                completed_at=completed_at,
                result_usable_at=completed_at + timedelta(minutes=1),
                outcome=outcome,
                duration_minutes=duration,
                series_id=f"series-{index // 2}",
                features={
                    "tempo": tempo,
                    "scaling": None if index % 6 == 0 else scaling,
                    "all_missing": None,
                },
            )
        )
    return tuple(rows)


class DraftModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = FeatureSchema.from_names(
            ("tempo", "scaling", "all_missing")
        )

    def test_schema_is_canonical_and_rejects_unknown_features(self) -> None:
        other = FeatureSchema.from_names(
            ("all_missing", "scaling", "tempo")
        )
        self.assertEqual(self.schema, other)
        self.assertEqual(self.schema.schema_hash, other.schema_hash)
        self.assertEqual(
            self.schema.names, ("all_missing", "scaling", "tempo")
        )

        with self.assertRaisesRegex(ValueError, "outside fixed schema"):
            fit_draft_model(
                (
                    replace(
                        training_rows(1)[0],
                        features={"tempo": 1.0, "unknown": 2.0},
                    ),
                ),
                self.schema,
                TRAINING_CUTOFF,
                10,
                min_samples=2,
            )

    def test_fit_is_deterministic_and_independent_of_row_order(self) -> None:
        rows = training_rows()
        first = fit_draft_model(
            rows,
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )
        second = fit_draft_model(
            reversed(rows),
            FeatureSchema.from_names(reversed(self.schema.names)),
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )

        self.assertEqual(first.status, ModelStatus.TRAINED)
        self.assertEqual(first, second)
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(len(first.feature_schema_hash), 64)
        self.assertEqual(len(first.training_input_hash), 64)
        self.assertEqual(len(first.model_hash), 64)

    def test_future_and_not_yet_completed_rows_cannot_change_model(self) -> None:
        rows = training_rows()
        baseline = fit_draft_model(
            rows,
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )
        future = DraftTrainingRow(
            match_id=90_001,
            input_snapshot_hash="a" * 64,
            cutoff=TRAINING_CUTOFF + timedelta(seconds=1),
            completed_at=TRAINING_CUTOFF + timedelta(minutes=50),
            result_usable_at=TRAINING_CUTOFF + timedelta(minutes=51),
            outcome=1,
            duration_minutes=50,
            series_id="future",
            features={"unknown_future_feature": float("inf")},
        )
        unfinished = DraftTrainingRow(
            match_id=90_002,
            input_snapshot_hash="b" * 64,
            cutoff=TRAINING_CUTOFF - timedelta(minutes=5),
            completed_at=TRAINING_CUTOFF + timedelta(minutes=45),
            result_usable_at=TRAINING_CUTOFF + timedelta(minutes=46),
            outcome=1,
            duration_minutes=50,
            series_id="unfinished",
            features={"tempo": 10_000.0},
        )
        changed = fit_draft_model(
            (*rows, future, unfinished),
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )

        self.assertEqual(changed, baseline)

    def test_result_availability_is_explicit_and_checked_before_features(self) -> None:
        rows = training_rows()
        baseline = fit_draft_model(
            rows,
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )
        unavailable = replace(
            rows[0],
            match_id=90_003,
            input_snapshot_hash="c" * 64,
            result_usable_at=None,
            features={"future_unknown": float("inf")},
        )
        late_result = replace(
            rows[1],
            match_id=90_004,
            input_snapshot_hash="d" * 64,
            result_usable_at=TRAINING_CUTOFF + timedelta(seconds=1),
            features={"future_unknown": float("inf")},
        )
        not_completed = replace(
            rows[2],
            match_id=90_005,
            input_snapshot_hash="e" * 64,
            cutoff=TRAINING_CUTOFF - timedelta(days=10),
            completed_at=TRAINING_CUTOFF + timedelta(seconds=1),
            result_usable_at=TRAINING_CUTOFF + timedelta(minutes=1),
            duration_minutes=20.0,
            features={"future_unknown": float("inf")},
        )

        changed = fit_draft_model(
            (*rows, unavailable, late_result, not_completed),
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )

        self.assertEqual(changed, baseline)

    def test_exact_duplicates_are_deduplicated_and_conflicts_rejected(self) -> None:
        rows = training_rows()
        baseline = fit_draft_model(
            rows,
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )
        deduplicated = fit_draft_model(
            (*rows, rows[0], rows[0]),
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )

        self.assertEqual(deduplicated, baseline)
        with self.assertRaisesRegex(ValueError, "conflicting training rows"):
            fit_draft_model(
                (*rows, replace(rows[0], outcome=1 - int(rows[0].outcome))),
                self.schema,
                TRAINING_CUTOFF,
                10,
                min_samples=10,
            )

    def test_training_hash_binds_feature_snapshot_identity(self) -> None:
        rows = training_rows()
        baseline = fit_draft_model(
            rows,
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )
        changed = fit_draft_model(
            (replace(rows[0], input_snapshot_hash="f" * 64), *rows[1:]),
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )

        self.assertNotEqual(changed.training_input_hash, baseline.training_input_hash)
        self.assertNotEqual(changed.model_hash, baseline.model_hash)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            fit_draft_model(
                (replace(rows[0], input_snapshot_hash="a" * 63 + " "), *rows[1:]),
                self.schema,
                TRAINING_CUTOFF,
                10,
                min_samples=10,
            )

    def test_horizon_uses_only_maps_that_reached_landmark(self) -> None:
        rows = training_rows()
        at_10 = fit_draft_model(
            rows,
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=2,
        )
        at_40 = fit_draft_model(
            rows,
            self.schema,
            TRAINING_CUTOFF,
            40,
            min_samples=2,
        )

        self.assertEqual(at_10.support, 40)
        self.assertEqual(at_40.support, 0)
        self.assertEqual(at_40.status, ModelStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(at_40.reason, "support_below_minimum")

    def test_map_ending_exactly_at_landmark_is_not_still_active(self) -> None:
        rows = training_rows(4)
        exact = tuple(replace(row, duration_minutes=10.0) for row in rows[:2])
        beyond = tuple(replace(row, duration_minutes=10.01) for row in rows[2:])

        model = fit_draft_model(
            (*exact, *beyond),
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=2,
        )

        self.assertEqual(model.support, 2)

    def test_missing_values_are_auditable_and_prediction_is_explainable(self) -> None:
        model = fit_draft_model(
            training_rows(),
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )
        payload = model.to_payload()
        json.dumps(payload, allow_nan=False)

        self.assertEqual(dict(model.missing_counts)["all_missing"], model.support)
        self.assertEqual(dict(model.imputation_values)["all_missing"], 0.0)
        self.assertEqual(dict(model.standardization_scales)["all_missing"], 1.0)
        self.assertEqual(dict(model.coefficients)["all_missing"], 0.0)

        prediction = predict_draft(
            model,
            {"tempo": 2.0, "scaling": None},
            top_n=3,
        )
        repeated = predict_draft(
            model,
            {"scaling": None, "tempo": 2.0},
            top_n=3,
        )
        self.assertEqual(prediction, repeated)
        self.assertEqual(prediction.status, PredictionStatus.PREDICTED)
        self.assertGreaterEqual(prediction.probability, 0.0)
        self.assertLessEqual(prediction.probability, 1.0)
        self.assertGreaterEqual(prediction.uncertainty, 0.0)
        self.assertLessEqual(prediction.uncertainty, 0.5)
        self.assertEqual(
            prediction.missing_features, ("all_missing", "scaling")
        )
        self.assertLessEqual(len(prediction.top_contributions), 3)
        self.assertEqual(
            tuple(
                sorted(
                    prediction.top_contributions,
                    key=lambda row: (
                        -abs(row.log_odds_contribution),
                        row.feature_name,
                    ),
                )
            ),
            prediction.top_contributions,
        )
        json.dumps(prediction.to_payload(), allow_nan=False)

    def test_prediction_rejects_a_model_whose_parameters_do_not_match_hash(self) -> None:
        model = fit_draft_model(
            training_rows(),
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )
        tampered = replace(model, intercept=float(model.intercept) + 100.0)

        with self.assertRaisesRegex(ValueError, "artifact hash"):
            predict_draft(tampered, {"tempo": 1.0})

    def test_small_or_single_class_training_returns_explicit_status(self) -> None:
        too_small = fit_draft_model(
            training_rows(3),
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )
        one_class_rows = tuple(
            replace(row, outcome=1) for row in training_rows(20)
        )
        one_class = fit_draft_model(
            one_class_rows,
            self.schema,
            TRAINING_CUTOFF,
            10,
            min_samples=10,
        )

        self.assertEqual(too_small.status, "insufficient_evidence")
        self.assertEqual(too_small.reason, "support_below_minimum")
        self.assertEqual(one_class.status, "insufficient_evidence")
        self.assertEqual(one_class.reason, "single_class_training_data")
        prediction = predict_draft(one_class, {})
        self.assertEqual(prediction.status, "insufficient_evidence")
        self.assertIsNone(prediction.probability)
        self.assertIsNone(prediction.uncertainty)


class DraftMetricTests(unittest.TestCase):
    def test_metrics_include_primary_secondary_and_equal_count_ece(self) -> None:
        metrics = evaluate_binary_predictions(
            (0, 0, 1, 1),
            (0.1, 0.4, 0.6, 0.9),
        )

        self.assertAlmostEqual(metrics.brier_score, 0.085)
        self.assertAlmostEqual(metrics.expected_calibration_error, 0.25)
        self.assertEqual(metrics.auc, 1.0)
        self.assertEqual(metrics.accuracy, 1.0)
        self.assertEqual(len(metrics.calibration_bins), 4)
        self.assertLess(metrics.log_loss, CALIBRATION_MAX_LOG_LOSS)

    def test_fixed_calibration_gate(self) -> None:
        outcomes = tuple(index % 2 for index in range(100))
        probabilities = tuple(
            0.95 - index / 100_000 if value else 0.05 + index / 100_000
            for index, value in enumerate(outcomes)
        )
        metrics = evaluate_binary_predictions(outcomes, probabilities)

        passed = passes_calibration_gate(metrics, ece_upper_bound=0.10)
        failed = passes_calibration_gate(metrics, ece_upper_bound=0.16)
        understated = passes_calibration_gate(metrics, ece_upper_bound=0.0)
        self.assertTrue(passed.passed)
        self.assertFalse(failed.passed)
        self.assertFalse(understated.passed)
        self.assertIn("ece_upper_bound_above_0.15", failed.reasons)
        self.assertIn(
            "ece_upper_bound_below_point_estimate", understated.reasons
        )

    def test_tied_probability_ece_is_invariant_to_row_permutation(self) -> None:
        probabilities = (0.5,) * 10
        grouped = evaluate_binary_predictions(
            (0, 0, 0, 0, 0, 1, 1, 1, 1, 1), probabilities
        )
        interleaved = evaluate_binary_predictions(
            (0, 1, 0, 1, 0, 1, 0, 1, 0, 1), probabilities
        )

        self.assertEqual(grouped, interleaved)
        self.assertEqual(grouped.expected_calibration_error, 0.0)
        self.assertEqual(len(grouped.calibration_bins), 1)

    def test_gate_rejects_non_five_bin_or_out_of_range_inputs(self) -> None:
        outcomes = tuple(index % 2 for index in range(100))
        probabilities = tuple(
            0.95 - index / 100_000 if value else 0.05 + index / 100_000
            for index, value in enumerate(outcomes)
        )
        metrics = evaluate_binary_predictions(outcomes, probabilities)

        missing_bins = passes_calibration_gate(
            replace(metrics, calibration_bins=()), ece_upper_bound=0.10
        )
        invalid_brier = passes_calibration_gate(
            replace(metrics, brier_score=float("nan")), ece_upper_bound=0.10
        )
        invalid_upper = passes_calibration_gate(
            metrics, ece_upper_bound=-0.01
        )

        self.assertIn(
            "calibration_bins_not_valid_five_bin_ece", missing_bins.reasons
        )
        self.assertIn("brier_out_of_range", invalid_brier.reasons)
        self.assertIn("ece_upper_bound_out_of_range", invalid_upper.reasons)
        self.assertFalse(missing_bins.passed)
        self.assertFalse(invalid_brier.passed)
        self.assertFalse(invalid_upper.passed)

    def test_auc_is_unavailable_for_single_class(self) -> None:
        metrics = evaluate_binary_predictions((1, 1), (0.6, 0.7))
        self.assertIsNone(metrics.auc)


class LandmarkSelectionTests(unittest.TestCase):
    def test_future_and_stale_landmarks_are_never_selected(self) -> None:
        candidates = (10, 20, 30)

        self.assertEqual(
            select_live_landmark(9.99, candidates).reason,
            "before_first_landmark",
        )
        at_17 = select_live_landmark(17, candidates)
        self.assertEqual(at_17.status, "selected")
        self.assertEqual(at_17.landmark_minute, 10)
        self.assertEqual(select_live_landmark(20, candidates).landmark_minute, 20)
        self.assertEqual(select_live_landmark(40, (20,)).landmark_minute, None)
        self.assertEqual(
            select_live_landmark(40, (20,)).reason,
            "validated_landmark_stale",
        )

    def test_unvalidated_landmark_is_ignored(self) -> None:
        selection = select_live_landmark(
            27,
            (
                LandmarkCandidate(10, True),
                LandmarkCandidate(20, False),
                LandmarkCandidate(30, True),
            ),
        )
        self.assertIsNone(selection.landmark_minute)
        self.assertEqual(selection.reason, "validated_landmark_stale")

    def test_landmark_validation_flag_must_be_boolean(self) -> None:
        with self.assertRaisesRegex(ValueError, "validated must be boolean"):
            LandmarkCandidate(10, "false")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
