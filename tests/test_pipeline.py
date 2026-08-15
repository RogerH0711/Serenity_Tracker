import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline


class PipelineRunnerTest(unittest.TestCase):
    def test_runs_each_stage_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "pipeline.lock"
            with (
                patch.object(pipeline, "LOCK_PATH", lock_path),
                patch("pipeline.subprocess.run") as run,
            ):
                self.assertEqual(pipeline.main(), 0)
                self.assertEqual(run.call_count, len(pipeline.PIPELINE_SCRIPTS))
                called_scripts = [Path(call.args[0][1]).name for call in run.call_args_list]
                self.assertEqual(called_scripts, list(pipeline.PIPELINE_SCRIPTS))

    def test_stops_after_failed_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "pipeline.lock"
            failure = subprocess.CalledProcessError(7, ["python", "scraper.py"])
            with (
                patch.object(pipeline, "LOCK_PATH", lock_path),
                patch(
                    "pipeline.subprocess.run", side_effect=[None, failure]
                ) as run,
            ):
                self.assertEqual(pipeline.main(), 7)
                self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
