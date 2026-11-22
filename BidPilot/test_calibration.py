import unittest

from scripts.synthetic_calibration import generate


class SyntheticCalibrationTests(unittest.TestCase):
    def test_pipeline_scores_three_models_on_held_out_jobs(self):
        report = generate(seed=7)
        self.assertTrue(report['synthetic'])
        self.assertEqual(report['held_out_projects'], 6)
        self.assertEqual(set(report['models']), {'ratecard', 'history', 'detailed'})
        for model in report['models'].values():
            overall = model['overall']
            self.assertEqual(overall['count'], 6)
            self.assertGreaterEqual(overall['median_absolute_hours_error'], 0)
            self.assertTrue(0 <= overall['underestimated_share'] <= 1)
        self.assertIsNotNone(report['models']['detailed']['overall']['median_absolute_cost_error_cents'])

    def test_seed_is_deterministic(self):
        self.assertEqual(generate(seed=7), generate(seed=7))


if __name__ == '__main__':
    unittest.main()